from textkit import slugify, title_case


def make_url(title: str) -> str:
    return f"/posts/{slugify(title)}"


if __name__ == "__main__":
    print(make_url(title_case("hello world")))
