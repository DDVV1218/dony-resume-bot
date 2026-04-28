# 基础镜像 - 使用官方 Python slim 镜像
FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 安装依赖（使用清华 PyPI 镜像加速）
COPY requirements.txt ./
RUN pip install --no-cache-dir \
    -i https://pypi.tuna.tsinghua.edu.cn/simple/ \
    -r requirements.txt

# 复制代码
COPY . .

# 创建数据目录（挂载点）
RUN mkdir -p /app/sessions /app/uploads /app/chroma_db

# 入口
ENTRYPOINT ["python", "main.py"]
