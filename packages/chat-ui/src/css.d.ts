// CSS Modules 的类型（消费方 Next 负责实际处理）
declare module "*.module.css" {
  const classes: Readonly<Record<string, string>>;
  export default classes;
}
declare module "*.css";
