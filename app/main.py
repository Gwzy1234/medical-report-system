"""
app/main.py
===============================================================================
医学影像报告模板管理系统 —— FastAPI 入口

启动方式（在项目根目录执行）：
    uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
或：
    python -m app.main

接口文档：
    http://127.0.0.1:8000/docs
前端页面：
    http://127.0.0.1:8000/

说明：本系统按毕业设计范围采用简化登录（登录成功返回用户信息，不签发 JWT）。
      生产环境应在登录后签发 Token，并在写接口上校验身份。
===============================================================================
"""

from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from app import database
from app.services import llm_service, pdf_service

# ---------------------------------------------------------------------------
# 日志与路径
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
# 降低 HTTP 客户端日志噪声，避免每条请求都打印一行 httpx 日志
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("medical_report")

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
INDEX_FILE = STATIC_DIR / "index.html"

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fa5]{3,32}$")


# ---------------------------------------------------------------------------
# 生命周期：启动时自动建表 / 写入初始提示词
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_: FastAPI):
    database.init_db()
    logger.info("数据库已就绪：%s", database.DB_PATH)
    if not llm_service.is_configured():
        logger.warning(
            "未配置 DEEPSEEK_API_KEY，/generate_conclusion 将返回 503；"
            "设置环境变量并重启服务即可启用大模型。"
        )
    yield
    logger.info("服务已停止")


app = FastAPI(
    title="医学影像报告模板管理系统",
    description=(
        "基于 FastAPI + SQLite + DeepSeek 的医学影像报告辅助生成系统。"
        "支持用户注册登录、按检查分类调用大模型生成诊断意见、"
        "保存患者报告并导出带宋体中文排版的 PDF。"
    ),
    version="1.0.0",
    lifespan=lifespan,
    openapi_tags=[
        {"name": "用户", "description": "注册与登录"},
        {"name": "提示词", "description": "检查分类与 System Prompt 配置"},
        {"name": "模板", "description": "报告模板的查询、新建与删除"},
        {"name": "报告", "description": "报告保存、查询与 PDF 导出"},
        {"name": "AI", "description": "调用 DeepSeek 生成诊断意见"},
        {"name": "系统", "description": "健康检查与首页"},
    ],
)

# 便于用 Live Server / 本地文件调试前端；生产环境请收敛 allow_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Pydantic 请求模型
# ---------------------------------------------------------------------------
class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=32, description="登录名")
    password: str = Field(..., min_length=6, max_length=64, description="明文密码，服务端做 SHA-256")

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        value = value.strip()
        if not USERNAME_PATTERN.fullmatch(value):
            raise ValueError("用户名需为 3~32 位中文、字母、数字或下划线")
        return value


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=32)
    password: str = Field(..., min_length=1, max_length=64)

    @field_validator("username")
    @classmethod
    def strip_username(cls, value: str) -> str:
        return value.strip()


class GenerateConclusionRequest(BaseModel):
    category: str = Field(..., min_length=1, max_length=50, description="检查分类")
    findings: str = Field(..., min_length=1, max_length=20000, description="影像所见")

    @field_validator("category", "findings")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不能为空")
        return value


class ReportCreateRequest(BaseModel):
    template_id: Optional[int] = Field(default=None, ge=1)
    patient_name: str = Field(..., min_length=1, max_length=50)
    patient_gender: str = Field(default="未知", max_length=10)
    patient_age: Optional[int] = Field(default=None, ge=0, le=150)
    patient_record_no: str = Field(default="", max_length=50)
    findings: str = Field(..., min_length=1, max_length=20000)
    conclusion: str = Field(..., min_length=1, max_length=20000)
    doctor_id: Optional[int] = Field(default=None, ge=1)

    @field_validator("template_id", "doctor_id", mode="before")
    @classmethod
    def empty_id_to_none(cls, value):
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("patient_age", mode="before")
    @classmethod
    def empty_age_to_none(cls, value):
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("patient_gender", mode="before")
    @classmethod
    def normalize_gender(cls, value):
        if value is None:
            return "未知"
        text = str(value).strip()
        return text or "未知"

    @field_validator("patient_name", "findings", "conclusion")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不能为空")
        return value


class TemplateCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=100)
    category: str = Field(..., min_length=1, max_length=50)
    findings: str = Field(default="", max_length=20000)
    conclusion: str = Field(default="", max_length=20000)
    is_ai_generated: bool = False
    creator_id: Optional[int] = Field(default=None, ge=1)

    @field_validator("title", "category")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不能为空")
        return value

    @field_validator("creator_id", mode="before")
    @classmethod
    def empty_id_to_none(cls, value):
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value


# ---------------------------------------------------------------------------
# 参数校验提示中文化（Pydantic 默认是英文）
# ---------------------------------------------------------------------------
_VALIDATION_MESSAGE_PATTERNS = (
    (r"String should have at least (\d+) characters", r"长度不能少于 \1 个字符"),
    (r"String should have at most (\d+) characters", r"长度不能超过 \1 个字符"),
    (r"Field required", "该字段为必填项"),
    (r"Input should be a valid integer.*", "必须是整数"),
    (r"Input should be a valid boolean.*", "必须是布尔值"),
    (r"Input should be a valid string", "必须是字符串"),
    (r"Input should be greater than or equal to (-?\d+)", r"不能小于 \1"),
    (r"Input should be less than or equal to (-?\d+)", r"不能大于 \1"),
)


def _translate_validation_message(message: str) -> str:
    for pattern, replacement in _VALIDATION_MESSAGE_PATTERNS:
        if re.fullmatch(pattern, message):
            return re.sub(pattern, replacement, message)
    return message


# ---------------------------------------------------------------------------
# 统一异常处理：让前端始终拿到 {"detail": "..."} 结构
# ---------------------------------------------------------------------------
@app.exception_handler(llm_service.LLMError)
async def handle_llm_error(_: Request, exc: llm_service.LLMError) -> JSONResponse:
    logger.warning("大模型调用异常：%s", exc)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": str(exc), "type": exc.__class__.__name__},
    )


@app.exception_handler(pdf_service.PDFGenerationError)
async def handle_pdf_error(_: Request, exc: pdf_service.PDFGenerationError) -> JSONResponse:
    logger.error("PDF 生成异常：%s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc), "type": "PDFGenerationError"},
    )


@app.exception_handler(sqlite3.IntegrityError)
async def handle_integrity_error(_: Request, exc: sqlite3.IntegrityError) -> JSONResponse:
    logger.warning("数据库约束冲突：%s", exc)
    return JSONResponse(
        status_code=400,
        content={
            "detail": "数据保存失败：关联数据不存在或违反唯一性约束。",
            "type": "IntegrityError",
        },
    )


@app.exception_handler(RequestValidationError)
async def handle_validation_error(
    _: Request, exc: RequestValidationError
) -> JSONResponse:
    first = (exc.errors() or [{}])[0]
    location = [
        str(part)
        for part in first.get("loc", [])
        if part not in ("body", "query", "path")
    ]
    field_name = ".".join(location)
    message = first.get("msg", "参数校验失败")
    if message.startswith("Value error, "):
        message = message[len("Value error, "):]
    message = _translate_validation_message(message)
    detail = f"{field_name}：{message}" if field_name else message
    return JSONResponse(
        status_code=422,
        content={"detail": detail, "field": field_name},
    )


# ---------------------------------------------------------------------------
# 用户模块
# ---------------------------------------------------------------------------
@app.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    tags=["用户"],
    summary="注册新用户",
)
def register(payload: RegisterRequest):
    try:
        user = database.create_user(payload.username, payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"message": "注册成功", "user": user}


@app.post("/login", tags=["用户"], summary="用户登录")
def login(payload: LoginRequest):
    user = database.authenticate_user(payload.username, payload.password)
    if user is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {"message": "登录成功", "user": user}


# ---------------------------------------------------------------------------
# 提示词 / 检查分类
# ---------------------------------------------------------------------------
@app.get("/prompt_configs", tags=["提示词"], summary="查询检查分类")
def list_prompt_configs(active_only: bool = Query(True, description="只返回启用项")):
    configs = database.list_prompt_configs(active_only=active_only)
    # 列表接口不返回 system_prompt，避免提示词内容无意义地暴露在前端
    return [
        {
            "id": item["id"],
            "category": item["category"],
            "is_active": bool(item["is_active"]),
        }
        for item in configs
    ]


# ---------------------------------------------------------------------------
# AI 生成诊断意见
# ---------------------------------------------------------------------------
@app.post("/generate_conclusion", tags=["AI"], summary="根据影像所见生成诊断意见")
def generate_conclusion(payload: GenerateConclusionRequest):
    """
    去 prompt_configs 表查询该分类的 System Prompt，
    拼接影像所见后调用 DeepSeek，返回诊断意见。
    """
    return llm_service.generate_conclusion(payload.category, payload.findings)


# ---------------------------------------------------------------------------
# 模板模块
# ---------------------------------------------------------------------------
@app.get("/templates", tags=["模板"], summary="查询模板列表")
def list_templates(
    category: Optional[str] = Query(None, max_length=50),
    keyword: Optional[str] = Query(None, max_length=50),
    creator_id: Optional[int] = Query(None, ge=1),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return database.list_templates(
        category=category,
        keyword=keyword,
        creator_id=creator_id,
        limit=limit,
        offset=offset,
    )


@app.post(
    "/templates",
    status_code=status.HTTP_201_CREATED,
    tags=["模板"],
    summary="新建模板",
)
def create_template(payload: TemplateCreateRequest):
    if payload.creator_id is not None and database.get_user_by_id(payload.creator_id) is None:
        raise HTTPException(status_code=400, detail="创建人不存在")

    try:
        return database.create_template(
            title=payload.title,
            category=payload.category,
            findings=payload.findings,
            conclusion=payload.conclusion,
            is_ai_generated=1 if payload.is_ai_generated else 0,
            creator_id=payload.creator_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/templates/{template_id}", tags=["模板"], summary="查询模板详情")
def get_template(template_id: int):
    template = database.get_template(template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    return template


@app.delete("/templates/{template_id}", tags=["模板"], summary="删除模板")
def delete_template(template_id: int):
    if not database.delete_template(template_id):
        raise HTTPException(status_code=404, detail="模板不存在")
    return {"message": "模板已删除", "id": template_id}


# ---------------------------------------------------------------------------
# 报告模块
# ---------------------------------------------------------------------------
@app.get("/reports", tags=["报告"], summary="查询报告列表")
def list_reports(
    doctor_id: Optional[int] = Query(None, ge=1),
    keyword: Optional[str] = Query(None, max_length=50),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return database.list_reports(
        doctor_id=doctor_id,
        keyword=keyword,
        limit=limit,
        offset=offset,
    )


@app.post(
    "/reports",
    status_code=status.HTTP_201_CREATED,
    tags=["报告"],
    summary="保存报告",
)
def create_report(payload: ReportCreateRequest):
    if payload.template_id is not None and database.get_template(payload.template_id) is None:
        raise HTTPException(status_code=400, detail="所选模板不存在")
    if payload.doctor_id is not None and database.get_user_by_id(payload.doctor_id) is None:
        raise HTTPException(status_code=400, detail="报告医生不存在")

    try:
        return database.create_report(
            patient_name=payload.patient_name,
            findings=payload.findings,
            conclusion=payload.conclusion,
            template_id=payload.template_id,
            patient_gender=payload.patient_gender,
            patient_age=payload.patient_age,
            patient_record_no=payload.patient_record_no,
            doctor_id=payload.doctor_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/reports/{report_id}/pdf", tags=["报告"], summary="导出报告 PDF")
def export_report_pdf(report_id: int):
    report = database.get_report_detail(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="报告不存在")

    pdf_bytes = pdf_service.generate_report_pdf(report)
    filename = f"medical_report_{report_id}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/reports/{report_id}", tags=["报告"], summary="查询报告详情")
def get_report(report_id: int):
    report = database.get_report_detail(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    return report


# ---------------------------------------------------------------------------
# 系统与首页
# ---------------------------------------------------------------------------
@app.get("/health", tags=["系统"], summary="健康检查")
def health_check():
    return {
        "status": "ok",
        "app": app.title,
        "version": app.version,
        "database": str(database.DB_PATH),
        "llm_configured": llm_service.is_configured(),
        "llm_model": llm_service.DEEPSEEK_MODEL,
    }


@app.get("/", include_in_schema=False)
def read_index():
    if not INDEX_FILE.is_file():
        raise HTTPException(
            status_code=404,
            detail="前端页面不存在，请检查 app/static/index.html",
        )
    return FileResponse(str(INDEX_FILE), media_type="text/html; charset=utf-8")


# 静态资源（CSS/JS/图片等）；index.html 由上面的根路由直接返回
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# 本地直接运行：python -m app.main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000, reload=False)
