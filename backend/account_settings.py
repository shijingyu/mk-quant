"""Framework execution settings are separate from strategy parameters."""
import math


def validate_leverage(manifest, value=None):
    requirements = manifest['account']
    low, high = (1, 1) if manifest['market'] == 'spot' else (requirements['leverage_min'], requirements['leverage_max'])
    if value is None:
        return int(low)
    if isinstance(value, bool):
        raise ValueError('杠杆必须是策略允许范围内的整数')
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('杠杆必须是策略允许范围内的整数') from None
    if not math.isfinite(number) or not number.is_integer() or not low <= number <= high:
        raise ValueError(f'杠杆必须为 {low}–{high} 范围内的整数，现货仅支持 1 倍')
    return int(number)


def configured_leverage(account, manifest):
    return validate_leverage(manifest, account.get('configured_leverage'))
