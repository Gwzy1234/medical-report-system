"""
app/services/pdf_service.py
===============================================================================
报告 PDF 生成服务（reportlab）

排版结构：
    大标题（医学影像诊断报告）
    → 患者信息栏（姓名/性别/年龄/病历号/模板/医生/编号/日期）
    → 影像所见
    → 诊断意见
    → 报告医师 / 报告日期
    → 页脚（声明 + 页码）

中文字体：
    优先使用 reportlab 的 TTFont 加载 C:/Windows/Fonts/simsun.ttc（宋体，
    subfontIndex=0）；若该文件不存在（例如部署到 Linux），自动降级为
    reportlab 内置的 STSong-Light，保证服务仍可导出中文 PDF。

对外函数：
    generate_report_pdf(report) -> bytes
    save_report_pdf(report, path) -> Path
===============================================================================
"""

from __future__ import annotations

import logging
import os
import threading
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm, mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Flowable,
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 页面常量
# ---------------------------------------------------------------------------
PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN_LEFT = 2.0 * cm
MARGIN_RIGHT = 2.0 * cm
MARGIN_TOP = 2.0 * cm
MARGIN_BOTTOM = 2.2 * cm
CONTENT_WIDTH = PAGE_WIDTH - MARGIN_LEFT - MARGIN_RIGHT      # 17 cm

TITLE_TEXT = "医学影像诊断报告"
SUBTITLE_TEXT = "MEDICAL IMAGING DIAGNOSTIC REPORT"

# 颜色
NAVY = colors.HexColor("#1F3A5F")
DARK = colors.HexColor("#222222")
GRAY = colors.HexColor("#6B7788")
LIGHT_BLUE = colors.HexColor("#EEF3F8")
BORDER = colors.HexColor("#C9D3E0")

# 中文字体候选路径：环境变量 > Windows 宋体 > 少量常见回退
WINDOWS_SIMSUN = Path("C:/Windows/Fonts/simsun.ttc")
FONT_CANDIDATES: List[Path] = [
    Path(os.getenv("PDF_FONT_PATH", "")) if os.getenv("PDF_FONT_PATH") else WINDOWS_SIMSUN,
    Path(os.getenv("WINDIR", "C:/Windows")) / "Fonts" / "simsun.ttc",
    Path("C:/Windows/Fonts/SimSun.ttf"),
    Path("/usr/share/fonts/truetype/simsun/simsun.ttc"),
    Path("/usr/share/fonts/truetype/arphic/uming.ttc"),
]

_font_name: Optional[str] = None
_font_lock = threading.Lock()


class PDFGenerationError(Exception):
    """PDF 生成失败。"""

    status_code = 500


# ---------------------------------------------------------------------------
# 字体
# ---------------------------------------------------------------------------
def _find_simsun() -> Optional[Path]:
    """返回第一个存在的宋体文件路径。"""
    for path in FONT_CANDIDATES:
        try:
            if path and path.is_file():
                return path
        except OSError:
            continue
    return None


def _ensure_chinese_font() -> str:
    """
    注册中文字体并返回字体名（进程内只注册一次）。

    优先 TTFont(simsun.ttc) → 失败或文件缺失时使用 STSong-Light。
    """
    global _font_name
    if _font_name:
        return _font_name

    with _font_lock:
        if _font_name:
            return _font_name

        font_path = _find_simsun()
        if font_path is not None:
            try:
                pdfmetrics.registerFont(
                    TTFont("SimSun", str(font_path), subfontIndex=0)
                )
                # 宋体没有独立的粗体/斜体文件，四个字重都映射到同一字体，
                # 避免 Paragraph 中的 <b> 标签因找不到字体而报错。
                pdfmetrics.registerFontFamily(
                    "SimSun",
                    normal="SimSun",
                    bold="SimSun",
                    italic="SimSun",
                    boldItalic="SimSun",
                )
                _font_name = "SimSun"
                logger.info("PDF 中文字体加载成功：%s", font_path)
                return _font_name
            except Exception:                                  # noqa: BLE001
                logger.exception("宋体加载失败，降级为 STSong-Light：%s", font_path)
        else:
            logger.warning(
                "未找到宋体文件 C:/Windows/Fonts/simsun.ttc，"
                "PDF 将使用 reportlab 内置 STSong-Light。"
            )

        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        _font_name = "STSong-Light"
        return _font_name


# ---------------------------------------------------------------------------
# 文本与样式工具
# ---------------------------------------------------------------------------
def _display(value: Any, default: str = "—") -> str:
    """把 None / 空白转成占位符。"""
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _display_age(value: Any) -> str:
    """年龄显示：45 → 45 岁；已带“岁”则不重复拼接。"""
    if value is None or str(value).strip() == "":
        return "—"
    text = str(value).strip()
    return text if "岁" in text else f"{text} 岁"


def _display_date(value: Any) -> str:
    """created_at 只显示日期部分（YYYY-MM-DD）。"""
    text = _display(value)
    return text[:10] if len(text) >= 10 else text


def _display_report_no(report: Mapping[str, Any]) -> str:
    report_id = report.get("id")
    try:
        return f"R{int(report_id):06d}"
    except (TypeError, ValueError):
        return "—"


def _rich_text(text: Any, default: str = "—") -> str:
    """
    转义 XML 特殊字符并保留换行，供 Paragraph 使用。
    """
    if text is None:
        return default
    raw = str(text).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return default
    raw = escape(raw)
    raw = raw.replace("\t", "&nbsp;" * 4)
    raw = raw.replace("\n", "<br/>")
    return raw


def _build_styles(font_name: str) -> Dict[str, ParagraphStyle]:
    return {
        "title": ParagraphStyle(
            name="ReportTitle",
            fontName=font_name,
            fontSize=22,
            leading=30,
            alignment=TA_CENTER,
            textColor=NAVY,
            spaceAfter=2 * mm,
        ),
        "subtitle": ParagraphStyle(
            name="ReportSubtitle",
            fontName="Helvetica",
            fontSize=8.5,
            leading=12,
            alignment=TA_CENTER,
            textColor=GRAY,
            spaceAfter=3 * mm,
        ),
        "section": ParagraphStyle(
            name="SectionTitle",
            fontName=font_name,
            fontSize=13,
            leading=22,
            textColor=NAVY,
            backColor=LIGHT_BLUE,
            borderPadding=(3, 7, 3, 7),
            spaceBefore=5 * mm,
            spaceAfter=3 * mm,
            keepWithNext=1,
        ),
        "body": ParagraphStyle(
            name="BodyText",
            fontName=font_name,
            fontSize=11.5,
            leading=21,
            textColor=DARK,
            alignment=TA_JUSTIFY,
            wordWrap="CJK",
            spaceAfter=2 * mm,
        ),
        "label": ParagraphStyle(
            name="InfoLabel",
            fontName=font_name,
            fontSize=10,
            leading=15,
            textColor=GRAY,
            alignment=TA_LEFT,
        ),
        "value": ParagraphStyle(
            name="InfoValue",
            fontName=font_name,
            fontSize=10.5,
            leading=15,
            textColor=DARK,
            alignment=TA_LEFT,
            wordWrap="CJK",
        ),
        "sign": ParagraphStyle(
            name="SignText",
            fontName=font_name,
            fontSize=10.5,
            leading=16,
            textColor=DARK,
        ),
        "sign_right": ParagraphStyle(
            name="SignTextRight",
            fontName=font_name,
            fontSize=10.5,
            leading=16,
            textColor=DARK,
            alignment=TA_RIGHT,
        ),
    }


def _patient_info_table(report: Mapping[str, Any], styles: Dict[str, ParagraphStyle]) -> Table:
    """患者信息栏：4 列 × 4 行，标签列浅蓝底。"""
    rows = [
        (
            "患者姓名", _display(report.get("patient_name")),
            "性　　别", _display(report.get("patient_gender"), "未知"),
        ),
        (
            "年　　龄", _display_age(report.get("patient_age")),
            "病历号", _display(report.get("patient_record_no")),
        ),
        (
            "报告模板", _display(report.get("template_title")),
            "报告医生", _display(report.get("doctor_name")),
        ),
        (
            "报告编号", _display_report_no(report),
            "报告日期", _display_date(report.get("created_at")),
        ),
    ]

    data: List[List[Flowable]] = []
    for label1, value1, label2, value2 in rows:
        data.append(
            [
                Paragraph(label1, styles["label"]),
                Paragraph(_rich_text(value1), styles["value"]),
                Paragraph(label2, styles["label"]),
                Paragraph(_rich_text(value2), styles["value"]),
            ]
        )

    table = Table(
        data,
        colWidths=[2.6 * cm, 5.9 * cm, 2.6 * cm, 5.9 * cm],
        hAlign="CENTER",
    )
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.5, BORDER),
                ("BACKGROUND", (0, 0), (0, -1), LIGHT_BLUE),
                ("BACKGROUND", (2, 0), (2, -1), LIGHT_BLUE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    return table


def _section(title: str, body: Any, styles: Dict[str, ParagraphStyle]) -> List[Flowable]:
    """生成“标题条 + 正文”两个 Flowable。"""
    return [
        Paragraph(title, styles["section"]),
        Paragraph(_rich_text(body), styles["body"]),
    ]


def _signature_block(report: Mapping[str, Any], styles: Dict[str, ParagraphStyle]) -> Table:
    doctor = _display(report.get("doctor_name"))
    date = _display_date(report.get("created_at"))
    table = Table(
        [
            [
                Paragraph(f"报告医师：{_rich_text(doctor)}", styles["sign"]),
                Paragraph(f"报告日期：{_rich_text(date)}", styles["sign_right"]),
            ]
        ],
        colWidths=[CONTENT_WIDTH * 0.5, CONTENT_WIDTH * 0.5],
    )
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return table


# ---------------------------------------------------------------------------
# 页眉页脚
# ---------------------------------------------------------------------------
def _make_page_callbacks(font_name: str, patient_name: str, record_no: str):
    """返回 onPage 回调，负责页脚（及第 2 页起的页眉）。"""

    def draw_page(canvas, doc) -> None:                        # type: ignore[no-untyped-def]
        canvas.saveState()

        # 第 2 页起加页眉，避免长报告翻页后失去患者标识
        if doc.page > 1:
            canvas.setFont(font_name, 8.5)
            canvas.setFillColor(GRAY)
            canvas.drawString(
                MARGIN_LEFT,
                PAGE_HEIGHT - 1.25 * cm,
                "医学影像诊断报告（续）",
            )
            canvas.drawRightString(
                PAGE_WIDTH - MARGIN_RIGHT,
                PAGE_HEIGHT - 1.25 * cm,
                f"{patient_name}　{record_no}".strip(),
            )
            canvas.setStrokeColor(BORDER)
            canvas.setLineWidth(0.5)
            canvas.line(
                MARGIN_LEFT,
                PAGE_HEIGHT - 1.5 * cm,
                PAGE_WIDTH - MARGIN_RIGHT,
                PAGE_HEIGHT - 1.5 * cm,
            )

        # 页脚横线
        canvas.setStrokeColor(BORDER)
        canvas.setLineWidth(0.5)
        canvas.line(MARGIN_LEFT, 1.55 * cm, PAGE_WIDTH - MARGIN_RIGHT, 1.55 * cm)

        canvas.setFont(font_name, 8)
        canvas.setFillColor(GRAY)
        canvas.drawString(
            MARGIN_LEFT,
            1.05 * cm,
            "本报告由医学影像报告模板管理系统生成，仅供临床参考。",
        )
        canvas.drawRightString(
            PAGE_WIDTH - MARGIN_RIGHT,
            1.05 * cm,
            f"第 {doc.page} 页",
        )
        canvas.restoreState()

    return draw_page


# ---------------------------------------------------------------------------
# 对外函数
# ---------------------------------------------------------------------------
def generate_report_pdf(report: Mapping[str, Any]) -> bytes:
    """
    根据报告字典生成 PDF，返回完整字节内容。

    report 需要的键（缺省字段会用“—”占位，不会抛异常）：
        id, patient_name, patient_gender, patient_age, patient_record_no,
        findings, conclusion, created_at, template_title, doctor_name
    """
    if not isinstance(report, Mapping):
        raise PDFGenerationError("报告数据格式不正确，无法生成 PDF。")

    font_name = _ensure_chinese_font()
    styles = _build_styles(font_name)

    patient_name = _display(report.get("patient_name"), "未填写")
    record_no = _display(report.get("patient_record_no"))
    buffer = BytesIO()

    try:
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            leftMargin=MARGIN_LEFT,
            rightMargin=MARGIN_RIGHT,
            topMargin=MARGIN_TOP,
            bottomMargin=MARGIN_BOTTOM,
            title=TITLE_TEXT,
            author=_display(report.get("doctor_name"), "医学影像科"),
            subject=f"患者 {patient_name} 的影像诊断报告",
            creator="医学影像报告模板管理系统",
        )

        story: List[Flowable] = [
            Paragraph(TITLE_TEXT, styles["title"]),
            Paragraph(SUBTITLE_TEXT, styles["subtitle"]),
            HRFlowable(
                width="100%",
                thickness=1.1,
                color=NAVY,
                spaceBefore=0,
                spaceAfter=5 * mm,
            ),
            _patient_info_table(report, styles),
        ]

        story.extend(_section("影像所见", report.get("findings"), styles))
        story.extend(_section("诊断意见", report.get("conclusion"), styles))

        story.append(Spacer(1, 10 * mm))
        story.append(_signature_block(report, styles))

        doc.build(
            story,
            onFirstPage=_make_page_callbacks(font_name, patient_name, record_no),
            onLaterPages=_make_page_callbacks(font_name, patient_name, record_no),
        )
        return buffer.getvalue()
    except PDFGenerationError:
        raise
    except Exception as exc:                                   # noqa: BLE001
        logger.exception("生成 PDF 失败：report_id=%s", report.get("id"))
        raise PDFGenerationError(f"生成 PDF 失败：{exc}") from exc
    finally:
        buffer.close()


def save_report_pdf(report: Mapping[str, Any], path: str) -> Path:
    """生成 PDF 并写入磁盘，便于本地调试或批量导出。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(generate_report_pdf(report))
    return target
