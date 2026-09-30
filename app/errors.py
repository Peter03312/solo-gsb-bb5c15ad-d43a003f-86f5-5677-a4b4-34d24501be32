"""统一业务错误：携带稳定错误码，映射为 4xx 响应。"""
from __future__ import annotations


class AppError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 409, details=None):  # noqa: ANN001
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


def batch_exists(batch_no: str) -> AppError:
    return AppError(
        "BATCH_NO_REUSED",
        f"批次编号 {batch_no!r} 已存在，批次编号不得复用",
        409,
    )


def batch_not_found(batch_no: str) -> AppError:
    return AppError(
        "SOURCE_NOT_FOUND",
        f"批次 {batch_no!r} 不存在",
        404,
    )


def batch_quarantined(batch_no: str) -> AppError:
    return AppError(
        "BATCH_QUARANTINED",
        f"批次 {batch_no!r} 已隔离，隔离批次不得再投料",
        409,
    )


def batch_not_suspect(batch_no: str) -> AppError:
    return AppError(
        "BATCH_NOT_SUSPECT",
        f"批次 {batch_no!r} 当前不是疑似批次（编号不存在或已复核撤销），无可撤销的疑似标记",
        409,
        details={"batch_no": batch_no},
    )


def insufficient(batch_no: str, requested: object, remaining: object) -> AppError:
    return AppError(
        "INSUFFICIENT_BALANCE",
        f"批次 {batch_no!r} 余量不足：请求 {requested}，剩余 {remaining}",
        409,
        details={"batch_no": batch_no, "requested": str(requested), "remaining": str(remaining)},
    )


def unit_mismatch(batch_no: str, expected: str, actual: str) -> AppError:
    return AppError(
        "UNIT_MISMATCH",
        f"批次 {batch_no!r} 单位为 {expected!r}，与申报单位 {actual!r} 不一致",
        422,
    )


# ------------------------------------------------------------- 生产单撤销


def production_not_found(output_no: str) -> AppError:
    return AppError(
        "PRODUCTION_NOT_FOUND",
        f"批次 {output_no!r} 不是任何生产单的产出，无可撤销/查询的生产单",
        404,
        details={"output_batch_no": output_no},
    )


def production_already_cancelled(output_no: str) -> AppError:
    return AppError(
        "PRODUCTION_ALREADY_CANCELLED",
        f"产出 {output_no!r} 的生产单已撤销，已撤销生产单不得重复撤销",
        409,
        details={"output_batch_no": output_no},
    )


def output_is_suspect(output_no: str) -> AppError:
    return AppError(
        "OUTPUT_SUSPECT",
        f"产出 {output_no!r} 当前为疑似批次，疑似批次不得撤销，须先经质检复核排除",
        409,
        details={"output_batch_no": output_no},
    )


def output_has_review(output_no: str) -> AppError:
    return AppError(
        "OUTPUT_HAS_REVIEW",
        f"产出 {output_no!r} 已有质检复核记录，存在复核记录的生产单不得撤销",
        409,
        details={"output_batch_no": output_no},
    )


def output_already_consumed(output_no: str) -> AppError:
    return AppError(
        "OUTPUT_ALREADY_CONSUMED",
        f"产出 {output_no!r} 已被后续生产投料，存在下游投料的产出不得撤销",
        409,
        details={"output_batch_no": output_no},
    )


def batch_cancelled(batch_no: str) -> AppError:
    return AppError(
        "BATCH_CANCELLED",
        f"批次 {batch_no!r} 是已撤销生产单的产出，已撤销产出不得投料",
        409,
        details={"batch_no": batch_no},
    )


def cancellation_not_found(output_no: str) -> AppError:
    return AppError(
        "CANCELLATION_NOT_FOUND",
        f"产出 {output_no!r} 的生产单尚未撤销，无撤销记录",
        404,
        details={"output_batch_no": output_no},
    )


# ------------------------------------------------------------- 原料预留


def reservation_exists(reservation_no: str) -> AppError:
    return AppError(
        "RESERVATION_NO_REUSED",
        f"预留编号 {reservation_no!r} 已存在，预留编号不得重复",
        409,
        details={"reservation_no": reservation_no},
    )


def reservation_not_found(reservation_no: str) -> AppError:
    return AppError(
        "RESERVATION_NOT_FOUND",
        f"预留单 {reservation_no!r} 不存在",
        404,
        details={"reservation_no": reservation_no},
    )


def reservation_deadline_past(deadline: object) -> AppError:
    return AppError(
        "RESERVATION_DEADLINE_NOT_FUTURE",
        f"预留截止时间 {deadline} 不在未来（以服务端当前时间为准）",
        422,
        details={"expires_at": str(deadline)},
    )


def reservation_expired(reservation_no: str, expires_at: object) -> AppError:
    return AppError(
        "RESERVATION_EXPIRED",
        f"预留单 {reservation_no!r} 已到期（截止 {expires_at}），到期预留已释放额度，不得再核销或取消",
        409,
        details={"reservation_no": reservation_no, "expires_at": str(expires_at)},
    )


def reservation_cancelled(reservation_no: str) -> AppError:
    return AppError(
        "RESERVATION_CANCELLED",
        f"预留单 {reservation_no!r} 已取消，已取消预留不得再核销或重复取消",
        409,
        details={"reservation_no": reservation_no},
    )


def reservation_consumed(reservation_no: str) -> AppError:
    return AppError(
        "RESERVATION_CONSUMED",
        f"预留单 {reservation_no!r} 已核销，已核销预留不得重复核销或取消",
        409,
        details={"reservation_no": reservation_no},
    )


def reservation_mismatch(reservation_no: str, field: str, expected: object, actual: object) -> AppError:
    return AppError(
        "RESERVATION_MISMATCH",
        f"投料内容与预留单 {reservation_no!r} 不一致：{field} 应为 {expected!r}，实际为 {actual!r}；"
        "预留生产的投料批次、数量与单位须与预留完全一致，整笔拒绝",
        422,
        details={
            "reservation_no": reservation_no,
            "field": field,
            "expected": str(expected),
            "actual": str(actual),
        },
    )


def available_insufficient(batch_no: str, requested: object, available: object) -> AppError:
    return AppError(
        "INSUFFICIENT_BALANCE",
        f"批次 {batch_no!r} 可用量不足：请求 {requested}，可用 {available}"
        "（可用量 = 实存 - 未取消且未到期的预留）",
        409,
        details={
            "batch_no": batch_no,
            "requested": str(requested),
            "available": str(available),
        },
    )
