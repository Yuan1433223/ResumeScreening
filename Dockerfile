# 语法参考：docker/dockerfile 1.x
# 后端镜像：FastAPI + uvicorn，端口 8017
# 构建：docker build -t resumescreening-backend .
#   可选开启 tesseract OCR（默认关闭，走 rapidocr-onnxruntime 纯 Python 回退）：
#   docker build --build-arg INSTALL_TESSERACT=1 -t resumescreening-backend .
FROM python:3.12-slim

# ---- 构建参数 ----
# INSTALL_TESSERACT=1 时 apt 安装 tesseract-ocr；默认 0，pytesseract 找不到二进制时
# 业务层会自动回退到 rapidocr-onnxruntime（见 src/parser.py）。
ARG INSTALL_TESSERACT=0

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ---- 系统依赖 ----
# rapidocr-onnxruntime / onnxruntime / opencv-python-headless 在 slim 镜像下需要：
#   libgomp1       -> libgomp.so.1（onnxruntime 导入必需，slim 默认不带）
#   libgl1         -> libGL.so.1（cv2 导入必需）
#   libglib2.0-0   -> libglib-2.0.so.0（cv2 导入必需）
# tesseract 仅在 INSTALL_TESSERACT=1 时安装（含中英文语言包；
# 不装时业务层自动回退 rapidocr-onnxruntime，见 src/parser.py）。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgomp1 \
        libgl1 \
        libglib2.0-0 \
        curl \
        ca-certificates \
    && if [ "$INSTALL_TESSERACT" = "1" ]; then \
        apt-get install -y --no-install-recommends \
            tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim; \
    fi \
    && rm -rf /var/lib/apt/lists/*

# ---- Python 依赖（先拷 requirements，利用层缓存） ----
COPY requirements.txt ./
RUN pip install -r requirements.txt

# ---- 拷源码 ----
# 注意：.dockerignore 已排除 .venv / data / output / 真实简历目录 / node_modules 等。
COPY . .

# 业务数据卷：SQLite 与 LangGraph 检查点落在 /app/data（见 src/config.py DATA_DIR）。
VOLUME ["/app/data"]

EXPOSE 8017

# 健康检查：与 app/api.py 的 GET /api/health 对齐。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8017/api/health || exit 1

# 容器内简历上传/扫描目录：src/config.py 中 RESUME_DIR = ROOT/"海外项目负责招聘简历"，
# ROOT 为 /app，因此容器内路径是 /app/海外项目负责招聘简历；
# 该目录由 docker-compose.yml 以命名/绑定卷挂载，不要在镜像内打包真实简历。
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8017"]
