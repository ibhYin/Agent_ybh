#!/bin/zsh
# ============================================================
#  智能体 MVP 一键启动（macOS 双击运行）
#  仅负责：环境激活 → 依赖检查 → 数据库初始化 → 启动服务 → 打开浏览器
#  LLM 配置请在页面右上角【设置】面板完成，无需编辑任何文件
# ============================================================

cd "$(dirname "$0")"

echo "=============================================="
echo "  智能体 MVP 启动中..."
echo "=============================================="

# ---------- 1. 激活 conda 环境 ----------
if [ -f /opt/anaconda3/etc/profile.d/conda.sh ]; then
    source /opt/anaconda3/etc/profile.d/conda.sh
    conda activate langchain 2>/dev/null
fi
if [ "$CONDA_DEFAULT_ENV" != "langchain" ]; then
    echo "[错误] 无法激活 conda 环境 langchain"
    echo "      请确认已安装 Anaconda 且存在该环境：conda create -n langchain python=3.10"
    read "?按回车键退出..."
    exit 1
fi
echo "[1/3] conda 环境 langchain 已激活"

# ---------- 2. 依赖检查 + 数据库初始化 ----------
pip install -q -r requirements.txt
echo "[2/3] 依赖检查完成"

python manage.py migrate >/dev/null 2>&1
echo "[3/3] 数据库初始化完成"

# ---------- 3. 端口检查并启动 ----------
if lsof -i :8000 >/dev/null 2>&1; then
    echo "[错误] 端口 8000 已被占用，可能已有服务在运行"
    echo "      请先关闭旧服务，或改用：python manage.py runserver 127.0.0.1:8001"
    read "?按回车键退出..."
    exit 1
fi

echo "----------------------------------------------"
echo "  服务已启动：http://127.0.0.1:8000"
echo "  首次使用：页面右上角【设置】→ 填入 LLM 配置"
echo "  停止服务：按 Ctrl+C"
echo "----------------------------------------------"
echo ""

(sleep 2 && open http://127.0.0.1:8000) &
python manage.py runserver 127.0.0.1:8000

echo ""
echo "服务已停止"
read "?按回车键关闭窗口..."
