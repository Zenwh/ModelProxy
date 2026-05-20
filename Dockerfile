# ModelProxy Demo - Nginx 静态资源镜像
# 仅打包静态 HTML/JS/CSS + Markdown PRD，无后端
#
# 使用：
#   docker build -t modelproxy-demo .
#   docker run -d -p 8080:80 --name modelproxy-demo modelproxy-demo
#   open http://localhost:8080

FROM nginx:alpine

# 元信息
LABEL org.opencontainers.image.title="ModelProxy Demo"
LABEL org.opencontainers.image.description="ModelProxy 产品 PRD + 高保真页面原型 Demo"
LABEL org.opencontainers.image.source="https://github.com/yourorg/modelproxy"
LABEL org.opencontainers.image.version="1.0.0"

# 拷贝静态资源
# /usr/share/nginx/html/        -> 入口门厅
# /usr/share/nginx/html/demo/   -> Platform Admin + Channel Console + PRD Viewer
# /usr/share/nginx/html/PRD/    -> PRD 原始 Markdown（被 prd-viewer 通过 fetch 读取）
# /usr/share/nginx/html/API.md  -> 已有 API 文档
# /usr/share/nginx/html/资源池切分方案.md
COPY demo/ /usr/share/nginx/html/demo/
COPY PRD/ /usr/share/nginx/html/PRD/
COPY API.md /usr/share/nginx/html/API.md
COPY 资源池切分方案.md /usr/share/nginx/html/资源池切分方案.md
COPY README.md /usr/share/nginx/html/README.md

# 根 / 重定向到 /demo/index.html
COPY docker/index-redirect.html /usr/share/nginx/html/index.html

# Nginx 配置
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf

# 健康检查
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -q -O- http://localhost/healthz || exit 1

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
