"""Input parsing and validation.

The whole batch is validated before any replacement runs, so one bad line
fails the entire batch rather than silently dropping data.

Required fields per line:
  * an anonymous session id: ``session_id`` (alternatively ``id``)
  * ``text``: the transcript text (string)

Line order, duplicated lines and all other JSON fields are preserved.
"""
import json


class ValidationError(Exception):
    """Raised when the uploaded file or its rules are unusable."""


def load_rules(rules_blob):
    """Validate the rule list structure (patterns are compiled in the worker).

    Expected form: ``[["pattern", "replacement"], ...]`` with both elements
    strings. Pattern *syntax* is deliberately not checked here: refusing a
    syntactically valid pattern because it might be slow is not allowed, and
    compilation inside the worker turns any genuine syntax error into a
    whole-batch failure.
    """
    if rules_blob is None or not rules_blob.strip():
        raise ValidationError("规则列表不能为空")
    try:
        rules = json.loads(rules_blob)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"规则不是合法 JSON：{exc}") from exc

    if not isinstance(rules, list) or not rules:
        raise ValidationError("规则必须是非空数组，形如 [[\"模式\", \"替换串\"], ...]")
    normalized = []
    for i, rule in enumerate(rules):
        if (
            not isinstance(rule, list)
            or len(rule) != 2
            or not all(isinstance(x, str) for x in rule)
        ):
            raise ValidationError(
                f"第 {i + 1} 条规则必须是包含两个字符串的数组 [模式, 替换串]"
            )
        normalized.append((rule[0], rule[1]))
    return normalized


def read_ndjson(path):
    """Read and validate a UTF-8 NDJSON file.

    Returns a list of (line_number, parsed_object, original_text) tuples.
    Raises ValidationError on the first problem, which fails the whole batch.
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise ValidationError(f"无法读取上传文件：{exc}") from exc

    if not raw:
        raise ValidationError("文件为空")

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"文件不是合法 UTF-8 编码：{exc}") from exc

    records = []
    # splitlines handles \n / \r\n / \r uniformly; a trailing newline does not
    # create a phantom blank line, but an interior blank line is a bad line.
    lines = text.splitlines()
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            raise ValidationError(f"第 {line_no} 行为空行，整批失败")
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"第 {line_no} 行不是合法 JSON：{exc}") from exc
        if not isinstance(obj, dict):
            raise ValidationError(f"第 {line_no} 行必须是 JSON 对象")

        session_id = obj.get("session_id", obj.get("id"))
        if not isinstance(session_id, str) or not session_id:
            raise ValidationError(
                f"第 {line_no} 行缺少字符串类型的 session_id（或 id）"
            )
        if "text" not in obj or not isinstance(obj["text"], str):
            raise ValidationError(f"第 {line_no} 行缺少字符串类型的 text 字段")

        records.append((line_no, obj, line))

    if not records:
        raise ValidationError("文件没有任何有效记录")
    return records
