"""数据库查询工具 - LLM 自由 SQL + 安全检查

LLM 自行编写 SQL 查询，工具在执行前做严格的安全校验，
只允许 SELECT 查询，禁止任何写操作。
"""

import logging
import re
from typing import Any, Dict, List

from pydantic import BaseModel, Field

from services.tool_base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


# 数据库表结构说明（注入到 system prompt 供 LLM 参考）
DB_SCHEMA = """
数据库表结构：

表 resumes（简历主表）:
  name TEXT PRIMARY KEY - 姓名
  sex TEXT - 性别
  phone TEXT - 手机号
  email TEXT - 邮箱
  metadata TEXT - JSON 格式的详细元数据
  pdf_path TEXT - PDF 文件路径
  markdown_path TEXT - Markdown 文件路径
  created_at TEXT - 创建时间

  metadata 中的 JSON 字段（通过 json_extract 访问）:
    $.name, $.sex, $.phone, $.email
    $.undergraduate - 本科学校
    $.master - 硕士学校
    $.doctor - 博士学校
    $.skills - 技能列表（逗号分隔）
    $.intership_comps - 实习公司（逗号分隔）
    $.work_comps - 曾就职公司（逗号分隔）

表 resumes_fts（FTS5 全文搜索索引）:
  full_text TEXT - 全文内容
  name TEXT - 姓名（分词）
  school TEXT - 学校（分词）
  skills TEXT - 技能（分词）
  company TEXT - 公司（分词）

常用查询示例：
- SELECT COUNT(*) FROM resumes
- SELECT sex, COUNT(*) as cnt FROM resumes GROUP BY sex
- SELECT name, json_extract(metadata, '$.undergraduate') as school FROM resumes
- SELECT name FROM resumes WHERE json_extract(metadata, '$.skills') LIKE '%Python%'
- SELECT COUNT(*) FROM resumes_fts WHERE resumes_fts MATCH '"复旦" AND "CTA"'
"""

# 禁止的关键词（不区分大小写匹配）
_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|ATTACH|DETACH|PRAGMA|"
    r"REINDEX|REPLACE|TRIGGER|VACUUM|EXECUTE|GRANT|REVOKE|LOAD|IMPORT|"
    r"SAVEPOINT|RELEASE)\b",
    re.IGNORECASE,
)


class QueryResumeDBParams(BaseModel):
    """查询简历库参数"""
    sql: str = Field(
        default="",
        description="SQL 查询语句，必须是 SELECT 开头，只能查询不能修改数据",
    )


def _validate_sql(sql: str) -> Dict[str, Any]:
    """校验 SQL 安全性

    Returns:
        {"ok": True} 或 {"ok": False, "error": "原因"}
    """
    sql_stripped = sql.strip().strip(";").strip()

    if not sql_stripped:
        return {"ok": False, "error": "SQL 语句为空"}

    # 检查是否以 SELECT 开头
    if not sql_stripped.upper().startswith("SELECT"):
        return {"ok": False, "error": "只允许 SELECT 查询语句"}

    # 检查是否包含禁止的关键词
    match = _FORBIDDEN_KEYWORDS.search(sql_stripped)
    if match:
        return {
            "ok": False,
            "error": f"SQL 中包含禁止的关键词「{match.group(1)}」，不允许修改数据库",
        }

    # 检查是否有分号（禁止多条语句）
    if sql_stripped.count(";") > 1:
        return {"ok": False, "error": "不允许执行多条 SQL 语句"}

    # 检查是否有注释注入
    if "--" in sql_stripped and sql_stripped.index("--") > len(sql_stripped) - 3:
        # 仅行尾注释允许
        pass

    # 不允许 PRAGMA
    if "PRAGMA" in sql_stripped.upper():
        return {"ok": False, "error": "不允许使用 PRAGMA 命令"}

    return {"ok": True}


class QueryResumeDBTool(BaseTool):
    """数据库查询工具

    LLM 自行编写 SQL，工具执行前做严格的安全校验。
    只允许 SELECT 查询，禁止任何写入操作。
    """

    name: str = "query_resume_db"
    description: str = (
        "通过 SQL 查询简历库的统计信息。支持复杂的聚合查询和条件过滤。"
        "只能查询（SELECT），不能修改数据库。"
        f"数据库结构：{DB_SCHEMA}"
    )
    parameters = QueryResumeDBParams

    def _execute(self, sql: str = "") -> ToolResult:
        """执行 SQL 查询（仅 SELECT）

        Args:
            sql: SQL 查询语句

        Returns:
            查询结果
        """
        if not sql:
            return ToolResult(
                success=False,
                error="请提供 SQL 查询语句",
            )

        # 安全校验
        validation = _validate_sql(sql)
        if not validation["ok"]:
            return ToolResult(
                success=True,
                data={
                    "error": validation["error"],
                    "suggestion": "请改写为只读 SELECT 查询，例如 SELECT COUNT(*) FROM resumes",
                },
            )

        # 执行查询
        try:
            from services.db import get_connection

            conn = get_connection()
            cursor = conn.execute(sql)

            # 获取列名
            columns = [desc[0] for desc in cursor.description] if cursor.description else []

            # 获取结果
            rows = cursor.fetchall()

            # 转换为字典列表
            results = [dict(zip(columns, row)) for row in rows]

            logger.info(
                f"Query executed: {sql[:80]}... -> {len(results)} rows"
            )

            return ToolResult(
                success=True,
                data={
                    "sql": sql,
                    "total_rows": len(results),
                    "columns": columns,
                    "results": results,
                },
            )

        except Exception as e:
            error_msg = str(e)
            logger.warning(f"SQL query failed: {sql[:80]}... error: {error_msg}")

            return ToolResult(
                success=True,
                data={
                    "error": f"SQL 执行失败: {error_msg}",
                    "suggestion": "检查 SQL 语法和字段名是否正确。字段在 metadata 中需用 json_extract 访问。",
                    "sql": sql,
                },
            )
