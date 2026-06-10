"""
OcrTool — 使用 Falcon-OCR 模型进行本地 OCR 文字提取。

基于 tiiuae/Falcon-OCR (0.3B 参数)，支持:
- ocr_plain: 全页 OCR，适合简单文档、照片、幻灯片、收据等
- ocr_layout: 布局感知 OCR，适合复杂多栏文档、学术论文、报告等

模型自动从 HuggingFace Hub 下载（首次约 600MB，支持国内镜像加速）。
需要 CUDA GPU 以获得最佳性能（CPU 可用但较慢）。
所有推理在本地完成，图片数据不会离开本机。

依赖: torch, transformers, torchvision, huggingface_hub, safetensors,
       tokenizers, Pillow
"""

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from AutoRUN_v1.tools.base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)

# ── 依赖包列表 ───────────────────────────────────────────────────────────────

_REQUIRED_PACKAGES = [
    "torch",
    "transformers",
    "torchvision",
    "huggingface_hub",
    "safetensors",
    "tokenizers",
    "Pillow",
]

# ── 模型单例 ────────────────────────────────────────────────────────────────

_ocr_engine: Optional[Any] = None
_model_loading: bool = False
_model_error: Optional[str] = None


def _check_dependencies() -> Optional[str]:
    """检查 OCR 所需依赖是否已安装。返回 None 表示 OK，否则返回错误信息。"""
    missing = []
    for pkg in _REQUIRED_PACKAGES:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        return (
            f"缺少依赖: {', '.join(missing)}\n\n"
            "请运行以下命令安装:\n"
            f"pip install {' '.join(missing)}"
        )
    return None


def _auto_install_dependencies() -> Optional[str]:
    """尝试自动安装缺失的依赖。返回 None 表示成功，否则返回错误信息。"""
    missing = []
    for pkg in _REQUIRED_PACKAGES:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)

    if not missing:
        return None

    logger.info("正在安装 OCR 依赖: %s", " ".join(missing))
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-q"] + missing,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("OCR 依赖安装完成")
        return None
    except subprocess.CalledProcessError as e:
        return f"自动安装依赖失败: {e}\n请手动运行: pip install {' '.join(missing)}"


def _download_model(
    model_id: str = "tiiuae/Falcon-OCR",
    local_dir: Optional[str] = None,
    endpoint: Optional[str] = None,
) -> str:
    """从 HuggingFace Hub 下载模型，自动回退镜像。

    下载流程:
    1. 如果指定了 HF_ENDPOINT 环境变量 → 直接使用，不切换
    2. 先尝试官方源 (huggingface.co)
    3. 若超时/连接失败 → 自动切换到国内镜像 (hf-mirror.com)
    4. 全部失败 → 给出明确报错和解决方案

    支持断点续传。

    Returns:
        模型本地目录路径
    """
    from huggingface_hub import snapshot_download

    if local_dir:
        local_path = Path(local_dir)
        if local_path.exists() and (local_path / "model.safetensors").exists():
            logger.info("使用本地模型: %s", local_path)
            return str(local_path)

    # 如果调用者指定了 endpoint，直接使用
    if endpoint:
        logger.info("从 %s 下载模型 %s ...", endpoint, model_id)
        return snapshot_download(
            repo_id=model_id,
            local_dir=local_dir,
            endpoint=endpoint,
            resume_download=True,
        )

    # 用户显式设置了 HF_ENDPOINT → 直接使用，不自动切换
    env_endpoint = os.environ.get("HF_ENDPOINT")
    if env_endpoint:
        logger.info("使用 HF_ENDPOINT=%s 下载模型 %s ...", env_endpoint, model_id)
        try:
            return snapshot_download(
                repo_id=model_id,
                local_dir=local_dir,
                endpoint=env_endpoint,
                resume_download=True,
            )
        except Exception as e:
            raise RuntimeError(
                f"从 HF_ENDPOINT={env_endpoint} 下载失败: {e}\n"
                f"请检查网络连接或尝试其他镜像源。"
            ) from e

    # ── 自动回退: 官方源 → 镜像源 ──────────────────────────────────────
    _MIRRORS = [
        ("https://huggingface.co", "HuggingFace 官方"),
        ("https://hf-mirror.com", "HF-Mirror (国内镜像)"),
    ]

    errors: list[str] = []
    for mirror_url, mirror_name in _MIRRORS:
        logger.info("尝试从 %s 下载模型 %s ...", mirror_name, model_id)
        try:
            return snapshot_download(
                repo_id=model_id,
                local_dir=local_dir,
                endpoint=mirror_url,
                resume_download=True,
            )
        except Exception as e:
            err_msg = f"  {mirror_name} ({mirror_url}): {_format_network_error(e)}"
            errors.append(err_msg)
            logger.warning("下载失败: %s", err_msg)
            continue

    # ── 全部失败 ───────────────────────────────────────────────────────
    error_detail = "\n".join(errors)
    raise RuntimeError(
        f"无法下载模型 {model_id}。所有源均无法连接。\n\n"
        f"错误详情:\n{error_detail}\n\n"
        "解决方案:\n"
        "1. 设置环境变量 HF_ENDPOINT=https://hf-mirror.com 强制使用镜像\n"
        "2. 手动下载模型到本地: huggingface-cli download tiiuae/Falcon-OCR --local-dir ./models/falcon-ocr\n"
        "   然后设置 AUTORUN_OCR_LOCAL_DIR=./models/falcon-ocr\n"
        "3. 配置代理: set HTTPS_PROXY=http://your-proxy:port\n"
        "4. 检查网络连接和防火墙设置"
    )


def _format_network_error(e: Exception) -> str:
    """格式化网络错误为简短可读信息。"""
    msg = str(e)
    # 提取关键信息
    if "timed out" in msg.lower() or "timeout" in msg.lower():
        return "连接超时"
    if "connection refused" in msg.lower():
        return "连接被拒绝"
    if "name resolution" in msg.lower() or "getaddrinfo" in msg.lower():
        return "DNS 解析失败"
    if "connecterror" in msg.lower() or "max retries" in msg.lower():
        return "无法连接"
    if "429" in msg:
        return "请求过多 (429)，请稍后重试"
    if "403" in msg:
        return "访问被拒绝 (403)"
    if "404" in msg:
        return "模型不存在 (404)"
    # 截断过长消息
    return msg[:120]


def load_ocr_model(
    model_id: Optional[str] = None,
    local_dir: Optional[str] = None,
    device: Optional[str] = None,
    dtype: str = "float32",
) -> tuple:
    """懒加载 Falcon-OCR 模型（单例模式）。

    Args:
        model_id: HuggingFace 模型 ID，默认 tiiuae/Falcon-OCR
        local_dir: 本地模型缓存目录（跳过下载）
        device: 设备（cuda/cpu），默认自动检测
        dtype: 数据类型

    Returns:
        (engine, tokenizer) tuple
    """
    global _ocr_engine, _model_loading, _model_error

    if _ocr_engine is not None:
        return _ocr_engine

    if _model_error:
        raise RuntimeError(f"OCR 模型加载失败: {_model_error}")

    if _model_loading:
        raise RuntimeError("OCR 模型正在加载中，请稍后再试")

    _model_loading = True

    try:
        # 1. 检查并安装依赖
        dep_error = _check_dependencies()
        if dep_error:
            install_error = _auto_install_dependencies()
            if install_error:
                raise ImportError(install_error)

        # 2. 下载模型
        import torch
        from AutoRUN_v1.tools.ocr_engine import (
            OCR_MODEL_ID,
            load_and_prepare_model,
            setup_torch_config,
        )
        from AutoRUN_v1.tools.ocr_engine.data import ImageProcessor
        from AutoRUN_v1.tools.ocr_engine.paged_ocr_inference import OCRInferenceEngine

        resolved_model_id = model_id or OCR_MODEL_ID
        resolved_local_dir = local_dir or os.environ.get("AUTORUN_OCR_LOCAL_DIR")

        if not resolved_local_dir:
            resolved_local_dir = _download_model(
                model_id=resolved_model_id,
                endpoint=os.environ.get("HF_ENDPOINT"),
            )

        # 3. 加载模型
        setup_torch_config()

        model, tokenizer, model_args = load_and_prepare_model(
            hf_model_id=resolved_model_id,
            hf_local_dir=resolved_local_dir,
            device=device or os.environ.get("AUTORUN_OCR_DEVICE"),
            dtype=dtype or os.environ.get("AUTORUN_OCR_DTYPE", "float32"),
            compile=True,
        )

        image_processor = ImageProcessor(patch_size=16, merge_size=1)
        engine = OCRInferenceEngine(
            model, tokenizer, image_processor, capture_cudagraph=True
        )

        _ocr_engine = (engine, tokenizer)
        _model_loading = False

        logger.info("Falcon-OCR 模型加载完成")
        return _ocr_engine

    except Exception as e:
        _model_loading = False
        _model_error = str(e)
        raise RuntimeError(f"OCR 模型加载失败: {e}") from e


def unload_ocr_model():
    """卸载 OCR 模型以释放显存。"""
    global _ocr_engine, _model_error
    if _ocr_engine is not None:
        del _ocr_engine
        _ocr_engine = None
    _model_error = None
    import gc
    gc.collect()
    try:
        import torch
        torch.cuda.empty_cache()
    except Exception:
        logger.debug("Failed to clear CUDA cache during OCR unload", exc_info=True)


# ── OCR Tool ─────────────────────────────────────────────────────────────────


class OcrTool(Tool):
    """使用本地 Falcon-OCR 模型从图片中提取文字。

    对非多模态模型特别有用——模型可以先 OCR 提取文字，
    再对文字内容进行推理分析，无需处理图片本身。
    也适合需要节约 Token 的场景（不发送图片到 API）。

    首次调用会:
    1. 自动安装缺失的 pip 依赖
    2. 从 HuggingFace 下载模型（约 600MB，支持断点续传）
    3. 加载模型到 GPU/CPU
    后续调用复用已加载的模型。
    """

    @property
    def name(self) -> str:
        return "OCR"

    @property
    def description(self) -> str:
        return """使用本地 Falcon-OCR 模型从图片中提取文字。

支持两种模式:
- plain: 全页 OCR，适合简单文档、照片、幻灯片、收据、发票
- layout: 布局感知 OCR，适合复杂多栏文档、学术论文、报告、密集页面

参数:
- image: 图片文件路径（必填，支持 PNG、JPEG、BMP、TIFF、WebP）
- mode: OCR 模式，"plain"（默认，全页OCR）或 "layout"（布局感知OCR）

首次调用自动下载模型（约 600MB，支持断点续传），
下载时先尝试官方源，超时/失败自动切换到国内镜像 hf-mirror.com。
也可通过环境变量 HF_ENDPOINT=https://hf-mirror.com 强制使用镜像。
模型在本地运行，图片不会上传到任何服务器。

适用场景:
- 让非多模态模型"看到"图片内容
- 从扫描文档/截图/表格中提取文字
- 节约 Token（不发送图片到 API）
- 处理敏感图片（数据不离开本机）"""

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "image": {
                    "type": "string",
                    "description": "图片文件路径（本地文件）",
                },
                "mode": {
                    "type": "string",
                    "enum": ["plain", "layout"],
                    "default": "plain",
                    "description": "OCR 模式: plain=全页OCR, layout=布局感知OCR",
                },
            },
            "required": ["image"],
        }

    def is_read_only(self, args: Dict[str, Any]) -> bool:
        return True

    def is_enabled(self) -> bool:
        return True

    async def call(self, args: Dict[str, Any], context: ToolContext) -> ToolResult:
        image_path = (args.get("image") or "").strip()
        mode = (args.get("mode") or "plain").strip()

        if not image_path:
            return ToolResult(
                data="错误: 请提供图片路径（image 参数）",
                is_error=True,
            )

        # 解析路径
        img_path = Path(image_path)
        if not img_path.is_absolute():
            img_path = Path(context.cwd or Path.cwd()) / img_path

        if not img_path.exists():
            return ToolResult(
                data=f"错误: 图片文件不存在: {img_path}",
                is_error=True,
            )
        if not img_path.is_file():
            return ToolResult(
                data=f"错误: 路径不是文件: {img_path}",
                is_error=True,
            )

        # 检查文件扩展名
        suffix = img_path.suffix.lower()
        supported = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp"}
        if suffix not in supported:
            return ToolResult(
                data=f"错误: 不支持的图片格式 '{suffix}'。支持: {', '.join(sorted(supported))}",
                is_error=True,
            )

        if mode not in ("plain", "layout"):
            return ToolResult(
                data=f"错误: 不支持的 OCR 模式 '{mode}'。可选: plain, layout",
                is_error=True,
            )

        try:
            from PIL import Image

            # 加载图片
            try:
                pil_image = Image.open(img_path).convert("RGB")
            except Exception as e:
                return ToolResult(
                    data=f"错误: 无法打开图片: {e}",
                    is_error=True,
                )

            w, h = pil_image.size

            # 检查图片尺寸（太大可能 OOM）
            max_pixels = 4096 * 4096
            if w * h > max_pixels:
                return ToolResult(
                    data=f"错误: 图片尺寸过大 ({w}x{h}, {w*h} px)。"
                         f"请缩小到 {max_pixels} px 以内",
                    is_error=True,
                )

            # 加载模型（首次会自动下载依赖和模型）
            try:
                engine, tokenizer = load_ocr_model()
            except RuntimeError as e:
                return ToolResult(
                    data=f"OCR 模型加载失败:\n{e}\n\n"
                          "环境配置指南:\n"
                          "1. 设置 HF_ENDPOINT=https://hf-mirror.com 强制使用国内镜像\n"
                          "2. 设置 AUTORUN_OCR_LOCAL_DIR=<路径> 使用已下载的模型\n"
                          "3. 设置 AUTORUN_OCR_DEVICE=cpu 强制使用 CPU（无 GPU 时）\n"
                          "4. 运行 pip install torch transformers torchvision "
                          "huggingface_hub safetensors tokenizers Pillow",
                    is_error=True,
                )

            # 执行 OCR
            import torch

            if mode == "layout":
                results = engine.generate_with_layout(
                    images=[pil_image],
                    use_tqdm=False,
                )
                elements = results[0]
                lines = []
                for i, elem in enumerate(elements):
                    text = elem["text"]
                    if text:
                        lines.append(
                            f"[区域{i+1}] [{elem['category']}] "
                            f"(bbox={elem['bbox']}, score={elem['score']:.3f}):\n{text}"
                        )
                output = "\n\n".join(lines) if lines else "（未检测到文字区域）"
            else:
                texts = engine.generate_plain(
                    images=[pil_image],
                    use_tqdm=False,
                )
                output = texts[0] if texts else "（未提取到文字）"

            return ToolResult(
                data=(
                    f"OCR 完成 ({mode}, 图片: {w}x{h})\n"
                    f"{'─' * 50}\n"
                    f"{output}"
                ),
                is_error=False,
            )

        except ImportError as e:
            return ToolResult(
                data=f"缺少依赖: {e}\n\n"
                      "自动安装失败，请手动安装:\n"
                      "pip install torch transformers torchvision "
                      "huggingface_hub safetensors tokenizers Pillow",
                is_error=True,
            )
        except Exception as e:
            return ToolResult(
                data=f"OCR 执行出错: {type(e).__name__}: {e}",
                is_error=True,
            )
