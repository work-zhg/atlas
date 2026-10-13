from textkit.strings import slugify


def anchors(headings):
    return [f"#{slugify(h)}" for h in headings]
