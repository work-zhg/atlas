from textkit import slugfy, title_case


def make_url(title: str) -> str:
    return f"/posts/{slugfy(title)}"


if __name__ == "__main__":
    print(make_url(title_case("hello world")))
