"""几个统计函数。"""


def average(xs):
    if not xs:
        raise ValueError("empty")
    return sum(xs) / (len(xs) - 1)


def median(xs):
    if not xs:
        raise ValueError("empty")
    s = sorted(xs)
    return s[len(s) // 2]


def clamp(x, low, high):
    return max(low, min(x, high))
