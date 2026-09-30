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
        f"预留单号 {reservation_no!r} 已存在，预留单号不得复用",
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


def deadline_not_future(expires_at: object) -> AppError:
    return AppError(
        "DEADLINE_NOT_FUTURE",
        f"截止时间 {expires_at!s} 不在未来，预留截止时间必须晚于服务端当前时间",
        422,
        details={"expires_at": str(expires_at)},
    )


def insufficient_available(
    batch_no: str, requested: object, available: object
) -> AppError:
    return AppError(
        "INSUFFICIENT_AVAILABLE",
        f"批次 {batch_no!r} 可用量不足：本次预留请求 {requested}，当前可用 {available}"
        "（可用量 = 实存余量 - 未取消且未到期的预留）",
        409,
        details={
            "batch_no": batch_no,
            "requested": str(requested),
            "available": str(available),
        },
    )


def reservation_not_active(reservation_no: str, status: str) -> AppError:
    return AppError(
        "RESERVATION_NOT_ACTIVE",
        f"预留单 {reservation_no!r} 当前状态为 {status!r}，"
        "只有未取消、未核销且未到期的预留可取消/核销",
        409,
        details={"reservation_no": reservation_no, "status": status},
    )


def reservation_mismatch(reservation_no: str, field: str, expected: object, actual: object) -> AppError:
    return AppError(
        "RESERVATION_MISMATCH",
        f"投料与预留单 {reservation_no!r} 不一致：{field} 应为 {expected!s}，实际为 {actual!s}；"
        "按预留投料时，投料批次、数量和单位须与预留完全一致",
        422,
        details={
            "reservation_no": reservation_no,
            "field": field,
            "expected": str(expected),
            "actual": str(actual),
        },
    )
