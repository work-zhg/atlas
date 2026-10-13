from textkit.strings import slugfy


def anchors(headings):
    return [f"#{slugfy(h)}" for h in headings]
