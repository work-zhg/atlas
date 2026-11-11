"use client";

import { Button, Result } from "antd";
import Link from "next/link";

export default function NotFound() {
  return (
    <Result
      status="404"
      title="页面不存在"
      subTitle="地址可能写错了，或者这个页面已经被移走。"
      extra={
        <Link href="/">
          <Button type="primary">回到首页</Button>
        </Link>
      }
      style={{ margin: "auto" }}
    />
  );
}
