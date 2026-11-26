# --- 构建阶段：Alpine + musl 静态编译（体积小、国内拉取快）---
FROM alpine:3.19 AS build
RUN sed -i 's#dl-cdn.alpinelinux.org#mirrors.aliyun.com#g' /etc/apk/repositories \
 && apk add --no-cache gcc musl-dev linux-headers
WORKDIR /src
COPY fanctl.c ./
# -static：musl 静态链接，产物可直接跑在任何 glibc 镜像里
RUN gcc -O2 -static -pthread -o fanctl fanctl.c

# --- 运行阶段 ---
FROM python:3.11-slim
WORKDIR /app
COPY --from=build /src/fanctl /app/fanctl
COPY start.sh /app/start.sh
COPY web/server.py /app/web/server.py
RUN chmod +x /app/fanctl /app/start.sh

ENV FAN_DATA=/data PORT=8090 TZ=Asia/Shanghai
VOLUME ["/data"]
EXPOSE 8090
CMD ["/app/start.sh"]
