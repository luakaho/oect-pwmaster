// fanctl - OEC fan controller with hot-reloadable config
// PWM: gpiochip3 line12 (LED-G) 25kHz software PWM
// TACH: gpiochip0 line19 (LED-R)   BLUE LED: gpiochip3 line11
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <signal.h>
#include <pthread.h>
#include <time.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <linux/gpio.h>

#define PWM_CHIP "/dev/gpiochip3"
#define PWM_LINE 12
#define TACH_CHIP "/dev/gpiochip0"
#define TACH_LINE 19
#define BLUE_CHIP "/dev/gpiochip3"
#define BLUE_LINE 11

static char g_data[220] = "/data";
static char g_logdir[260], g_cfgfile[260], g_manual[260], g_status[260];
static void init_paths(void) {
    const char *d = getenv("FAN_DATA");
    if (d && *d) { strncpy(g_data, d, sizeof g_data - 1); }
    snprintf(g_logdir,  sizeof g_logdir,  "%s", g_data);
    snprintf(g_cfgfile,  sizeof g_cfgfile,  "%s/config/fan.conf", g_data);
    snprintf(g_manual,   sizeof g_manual,   "%s/manual.duty", g_data);
    snprintf(g_status,   sizeof g_status,   "%s/status.json", g_data);
}
#define LEDS_DRV "/sys/bus/platform/drivers/leds-gpio"
#define LEDS_DEV "gpio-leds"
#define FREQ 25000
#define MAXPTS 6

typedef struct { double t[MAXPTS], d[MAXPTS]; int n; } curve_t;

static curve_t g_cc = {{42,50,60,70,0,0},{0,30,65,100,0,0},4};
static curve_t g_hc = {{40,45,50,55,0,0},{0,25,60,100,0,0},4};
static double g_hyst_start = 42, g_hyst_stop = 39;
static int g_min_duty = 0, g_max_duty = 100, g_failsafe = 60;
static int g_hdd_mode = 0;
static int g_update_ms = 2000;   /* control loop period */
static double g_boost_gain = 15.0, g_boost_max = 30.0;
static int g_deadband = 12;   /* 低于该占空比直接判 0（风扇也转不起来） */
static int g_freq = 15000;      /* PWM 频率 Hz */
static int g_spin_us = 5;       /* 每个边沿前自旋多少微秒 */
/* 温度趋势（用于预测加力） */
#define THIST 64
static double hist_t[THIST], hist_c[THIST];
static int hist_idx = 0, hist_n = 0;
static time_t g_cfg_mtime = 0;

static volatile int g_duty = 0, g_run = 1, g_rpm = -1;
static int pwm_fd = -1, tach_fd = -1, blue_fd = -1;

static int req_line(const char *chip, unsigned line, int out, const char *label) {
    int fd = open(chip, O_RDWR);
    if (fd < 0) { fprintf(stderr, "open %s: %s\n", chip, strerror(errno)); return -1; }
    struct gpiohandle_request r; memset(&r, 0, sizeof r);
    r.lineoffsets[0] = line;
    r.flags = out ? GPIOHANDLE_REQUEST_OUTPUT : GPIOHANDLE_REQUEST_INPUT;
    r.lines = 1;
    strncpy(r.consumer_label, label, sizeof(r.consumer_label)-1);
    if (ioctl(fd, GPIO_GET_LINEHANDLE_IOCTL, &r) < 0) {
        fprintf(stderr, "request %s:%u: %s\n", chip, line, strerror(errno));
        close(fd); return -1;
    }
    close(fd); return r.fd;
}
static int set_val(int fd, int v) {
    struct gpiohandle_data d; memset(&d, 0, sizeof d); d.values[0] = v ? 1 : 0;
    return ioctl(fd, GPIOHANDLE_SET_LINE_VALUES_IOCTL, &d);
}
static int get_val(int fd) {
    struct gpiohandle_data d; memset(&d, 0, sizeof d);
    if (ioctl(fd, GPIOHANDLE_GET_LINE_VALUES_IOCTL, &d) < 0) return -1;
    return d.values[0];
}
static void wr(const char *path, const char *val) {
    int fd = open(path, O_WRONLY); if (fd < 0) return;
    (void)write(fd, val, strlen(val)); close(fd);
}
static long long now_ns(void){ struct timespec ts; clock_gettime(CLOCK_MONOTONIC,&ts); return ts.tv_sec*1000000000LL+ts.tv_nsec; }
static void busy_until(long long t){
    long long n = now_ns();
    long long remain = t - n;
    long long spin = (long long)g_spin_us * 1000LL;
    if (remain > spin + 30000) {
        long long sleep_ns = remain - spin - 5000;
        struct timespec ts;
        ts.tv_sec  = sleep_ns / 1000000000LL;
        ts.tv_nsec = sleep_ns % 1000000000LL;
        nanosleep(&ts, NULL);
    }
    while (now_ns() < t) { }
}
static double read_t(const char *p){ FILE*f=fopen(p,"r"); if(!f) return -1; long v=0; if(fscanf(f,"%ld",&v)!=1){fclose(f);return -1;} fclose(f); return v/1000.0; }

static double curve_val(const curve_t *c, double t) {
    if (c->n <= 0) return 0;
    if (t <= c->t[0]) return c->d[0];
    if (t >= c->t[c->n-1]) return c->d[c->n-1];
    for (int i = 1; i < c->n; i++)
        if (t <= c->t[i]) {
            double t0=c->t[i-1], d0=c->d[i-1], t1=c->t[i], d1=c->d[i];
            if (t1 == t0) return d1;
            return d0 + (d1-d0)*(t-t0)/(t1-t0);
        }
    return c->d[c->n-1];
}
static void parse_curve(curve_t *c, const char *s) {
    curve_t tmp; memset(&tmp, 0, sizeof tmp);
    char buf[512]; strncpy(buf, s, sizeof buf - 1);
    char *save1 = NULL;
    for (char *tok = strtok_r(buf, ",", &save1); tok && tmp.n < MAXPTS; tok = strtok_r(NULL, ",", &save1)) {
        char *colon = strchr(tok, ':');
        if (!colon) continue;
        *colon = 0;
        double t = atof(tok), d = atof(colon+1);
        if (t < 10 || t > 95) continue;
        if (d < 0) d = 0; if (d > 100) d = 100;
        tmp.t[tmp.n] = t; tmp.d[tmp.n] = d; tmp.n++;
    }
    /* sort by temp */
    for (int i = 0; i < tmp.n; i++)
        for (int j = i+1; j < tmp.n; j++)
            if (tmp.t[j] < tmp.t[i]) {
                double tt=tmp.t[i]; tmp.t[i]=tmp.t[j]; tmp.t[j]=tt;
                double dd=tmp.d[i]; tmp.d[i]=tmp.d[j]; tmp.d[j]=dd;
            }
    if (tmp.n >= 2) *c = tmp;
}
static int load_cfg(void) {
    struct stat st;
    if (stat(g_cfgfile, &st) != 0) return 0;
    if (st.st_mtime == g_cfg_mtime) return 0;
    FILE *f = fopen(g_cfgfile, "r");
    if (!f) return 0;
    char line[1024]; int changed = 0;
    while (fgets(line, sizeof line, f)) {
        char *nl = strchr(line, '\n'); if (nl) *nl = 0;
        char *eq = strchr(line, '='); if (!eq) continue;
        *eq = 0;
        char key[64], val[512];
        snprintf(key, sizeof key, "%s", line);
        snprintf(val, sizeof val, "%s", eq+1);
        for (char *p = key; *p; p++) if (*p==' '||*p=='\t') { memmove(p, p+1, strlen(p)); p--; }
        char *v = val; while (*v==' '||*v=='\t') v++;
        if (!strcmp(key, "cpu_curve")) { parse_curve(&g_cc, v); changed = 1; }
        else if (!strcmp(key, "hdd_curve")) { parse_curve(&g_hc, v); changed = 1; }
        else if (!strcmp(key, "hyst_start")) { g_hyst_start = atof(v); changed = 1; }
        else if (!strcmp(key, "hyst_stop"))  { g_hyst_stop  = atof(v); changed = 1; }
        else if (!strcmp(key, "min_duty"))   { g_min_duty = atoi(v); changed = 1; }
        else if (!strcmp(key, "max_duty"))   { g_max_duty = atoi(v); changed = 1; }
        else if (!strcmp(key, "failsafe_duty")) { g_failsafe = atoi(v); changed = 1; }
        else if (!strcmp(key, "hdd_mode"))   { g_hdd_mode = (!strcmp(v,"avg"))?1:((!strcmp(v,"each"))?2:0); changed = 1; }
        else if (!strcmp(key, "update_sec")) { g_update_ms = atoi(v)*1000; changed = 1; }
        else if (!strcmp(key, "boost_gain")) { g_boost_gain = atof(v); changed = 1; }
        else if (!strcmp(key, "boost_max"))  { g_boost_max  = atof(v); changed = 1; }
        else if (!strcmp(key, "freq"))       { g_freq = atoi(v); changed = 1; }
        else if (!strcmp(key, "spin_us"))    { g_spin_us = atoi(v); changed = 1; }
        else if (!strcmp(key, "deadband_duty")) { g_deadband = atoi(v); changed = 1; }
    }
    fclose(f);
    if (g_min_duty < 0) g_min_duty = 0; if (g_min_duty > 100) g_min_duty = 100;
    if (g_max_duty < 0) g_max_duty = 0; if (g_max_duty > 100) g_max_duty = 100;
    if (g_max_duty < g_min_duty) g_max_duty = g_min_duty;
    if (g_update_ms < 1000) g_update_ms = 1000;
    if (g_update_ms > 60000) g_update_ms = 60000;
    if (g_boost_gain < 0) g_boost_gain = 0;
    if (g_boost_max < 0) g_boost_max = 0;
    if (g_boost_max > 60) g_boost_max = 60;
    if (g_freq < 1000) g_freq = 1000;
    if (g_freq > 30000) g_freq = 30000;
    if (g_spin_us < 0) g_spin_us = 0;
    if (g_spin_us > 20) g_spin_us = 20;
    if (g_deadband < 0) g_deadband = 0;
    if (g_deadband > 40) g_deadband = 40;
    g_cfg_mtime = st.st_mtime;
    return changed;
}
static void wait_for(long long dur_ns){
    long long start = now_ns();
    long long spin = (long long)g_spin_us * 1000LL;
    if (dur_ns > spin + 30000) {
        long long sl = dur_ns - spin - 5000;
        struct timespec ts;
        ts.tv_sec  = sl / 1000000000LL;
        ts.tv_nsec = sl % 1000000000LL;
        nanosleep(&ts, NULL);
    }
    while (now_ns() - start < dur_ns) { }
}
static void *pwm_thread(void *a) {
    (void)a;
    while (g_run) {
        int d = g_duty;
        double period = 1e9 / (g_freq > 0 ? g_freq : 15000);
        if (d <= 0)   { set_val(pwm_fd,0); usleep(1000); continue; }
        if (d >= 100) { set_val(pwm_fd,1); usleep(1000); continue; }
        long long on = (long long)(period*d/100.0), off = (long long)period - on;
        set_val(pwm_fd,1); wait_for(on);
        set_val(pwm_fd,0); wait_for(off);
    }
    set_val(pwm_fd,0); return NULL;
}
static void *tach_thread(void *a) {
    (void)a;
    while (g_run) {
        int pulses = 0, last = tach_fd >= 0 ? get_val(tach_fd) : 1;
        long long end = now_ns() + 500000000LL;
        while (now_ns() < end && g_run) {
            int v = tach_fd >= 0 ? get_val(tach_fd) : 1;
            if (v != last && v == 0) pulses++;
            last = v;
            struct timespec ts = {0, 500000}; nanosleep(&ts, NULL);
        }
        g_rpm = pulses * 60;   /* 2 pulses/rev, 0.5s window: pulses*2*60/2 */
    }
    return NULL;
}
static void *ctrl_thread(void *a) {
    (void)a;
    char logp[300]; snprintf(logp, sizeof logp, "%s/fan.log", g_logdir);
    
    int fan_on = 0; int cfg_reload = 0;
    while (g_run) {
        cfg_reload = load_cfg();
        double cpu = read_t("/sys/class/thermal/thermal_zone0/temp");
        double d1 = read_t("/sys/class/hwmon/hwmon2/temp1_input");
        double d2 = read_t("/sys/class/hwmon/hwmon3/temp1_input");
        double d3 = read_t("/sys/class/hwmon/hwmon4/temp1_input");
        double hdd;
        if (g_hdd_mode == 1) hdd = (d1+d2+d3)/3.0;
        else { hdd = d1; if (d2 > hdd) hdd = d2; if (d3 > hdd) hdd = d3; }
        int duty;
        if (cpu < 0) duty = g_failsafe;
        else {
            if (cpu >= g_hyst_start) fan_on = 1;
            else if (cpu <= g_hyst_stop) fan_on = 0;
            double dc = fan_on ? curve_val(&g_cc, cpu) : 0;
            double dh = (hdd > 0) ? curve_val(&g_hc, hdd) : 0;
            duty = (int)(dc > dh ? dc : dh);
            /* 趋势加力：记录历史并计算最近 30 秒斜率（℃/min） */
            hist_t[hist_idx] = (double)time(NULL);
            hist_c[hist_idx] = cpu;
            hist_idx = (hist_idx + 1) % THIST;
            if (hist_n < THIST) hist_n++;
            double boost = 0.0;
            if (hist_n >= 3) {
                int oldest = (hist_idx - hist_n + THIST) % THIST;
                double dt = hist_t[(hist_idx-1+THIST)%THIST] - hist_t[oldest];
                double dT = cpu - hist_c[oldest];
                if (dt >= 20.0) {
                    double slope = dT / (dt/60.0);      /* ℃/min */
                    if (slope > 0) {
                        boost = slope * g_boost_gain;
                        if (boost > g_boost_max) boost = g_boost_max;
                    }
                }
            }
            if (boost > 0 && duty > 0) duty = (int)(duty + boost + 0.5);
            if (duty < g_min_duty) duty = g_min_duty;
            if (duty > g_max_duty) duty = g_max_duty;
        }
        if (duty < g_deadband) duty = 0;   /* 死区：无效档位直接停转，省 CPU */
        FILE *mf = fopen(g_manual, "r");
        if (mf) { int mv; if (fscanf(mf, "%d", &mv) == 1 && mv >= 0 && mv <= 100) duty = mv; fclose(mf); }
        if (duty < 0) duty = 0; if (duty > 100) duty = 100;
        g_duty = duty;
        time_t tt = time(NULL); struct tm tm; localtime_r(&tt, &tm);
        char ts[32]; strftime(ts, sizeof ts, "%F %T", &tm);
        FILE *lf = fopen(logp, "a");
        if (lf) { fprintf(lf, "%s,cpu=%.0f,d1=%.0f,d2=%.0f,d3=%.0f,duty=%d,rpm=%d\n", ts,cpu,d1,d2,d3,duty,g_rpm); fclose(lf); }
        FILE *sf = fopen(g_status, "w");
        if (sf) { fprintf(sf, "{\"time\":\"%s\",\"cpu_c\":%.1f,\"d1_c\":%.1f,\"d2_c\":%.1f,\"d3_c\":%.1f,\"duty\":%d,\"rpm\":%d,\"freq\":%d,\"mode\":\"%s\",\"min_duty\":%d,\"max_duty\":%d,\"hyst_start\":%.0f,\"hyst_stop\":%.0f,\"fail\":%d,\"update_ms\":%d,\"boost_gain\":%.1f,\"boost_max\":%.1f,\"freq\":%d,\"spin_us\":%d,\"deadband_duty\":%d}\n",
                    ts, cpu, d1, d2, d3, duty, g_rpm, FREQ, access(g_manual,F_OK)==0?"手动":"自动", g_min_duty, g_max_duty, g_hyst_start, g_hyst_stop, g_failsafe, g_update_ms, g_boost_gain, g_boost_max, g_freq, g_spin_us, g_deadband); fclose(sf); }
        for (int slept = 0; slept < g_update_ms && g_run; slept += 100) usleep(100000);
    }
    return NULL;
}
static void on_sig(int s){ (void)s; g_run = 0; }
int main(void) {
    signal(SIGTERM, on_sig); signal(SIGINT, on_sig);
    init_paths();
    mkdir(g_logdir, 0755);
    char cfgdir[280];
    snprintf(cfgdir, sizeof cfgdir, "%s/config", g_data);
    mkdir(cfgdir, 0755);
    wr(LEDS_DRV "/unbind", LEDS_DEV); usleep(300000);
    load_cfg();
    pwm_fd  = req_line(PWM_CHIP,  PWM_LINE,  1, "fanctl-pwm");
    blue_fd = req_line(BLUE_CHIP, BLUE_LINE, 1, "fanctl-led");
    /* SAFE MODE: TACH pin not requested */
    if (pwm_fd < 0) { fprintf(stderr, "PWM unavailable\n"); goto out; }
    if (blue_fd >= 0) set_val(blue_fd, 0);
    fprintf(stderr, "fanctl: SAFE MODE, TACH pin untouched\n");
    pthread_t a,b,c;
    pthread_create(&a,NULL,pwm_thread,NULL);
    /* SAFE MODE: tach thread disabled */
    pthread_create(&c,NULL,ctrl_thread,NULL);
    while (g_run) sleep(1);
    pthread_join(a,NULL); pthread_join(b,NULL); pthread_join(c,NULL);
out:
    if (pwm_fd>=0) { set_val(pwm_fd,0); close(pwm_fd); }
    if (tach_fd>=0) close(tach_fd);
    if (blue_fd>=0) { set_val(blue_fd,1); close(blue_fd); }
    wr(LEDS_DRV "/bind", LEDS_DEV);
    return 0;
}
