# 前端多阶段构建：node:22-alpine 编译 dist -> nginx:alpine 托管
# 构建：docker build -t resumescreening-web ./frontend

# ---- 阶段 1：构建 ----
FROM node:22-alpine AS builder
WORKDIR /build

# 先拷 lock + package 利用缓存；有 lock 用 npm ci，无 lock 退化为 npm install
COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

# 拷源码并构建（tsc --noEmit && vite build，产物输出到 dist/）
COPY . .
RUN npm run build

# ---- 阶段 2：运行 ----
FROM nginx:alpine
# 用本目录 nginx.conf 替换默认站点配置
COPY nginx.conf /etc/nginx/conf.d/default.conf
# 拷贝构建产物
COPY --from=builder /build/dist /usr/share/nginx/html

EXPOSE 80
