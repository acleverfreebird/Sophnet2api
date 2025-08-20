# Sophnet2API

Sophnet2API 是一个用于处理认证的应用程序，使用Camoufox进行异步浏览器认证。

## Docker 镜像

Docker 镜像已发布到 Docker Hub，名称为 `freebird2913/sophnet2api`。

## 构建和运行

要构建和运行 Docker 镜像，请执行以下步骤：

1. 克隆此仓库到本地：

   ```bash
   git clone https://github.com/acleverfreebird/sophnet2api.git
   cd sophnet2api
   ```

2. 构建 Docker 镜像：

   ```bash
   docker build -t freebird2913/sophnet2api .
   ```

3. 运行 Docker 容器：

   ```bash
   docker run --rm freebird2913/sophnet2api
   ```

## 使用示例

运行容器后，应用程序将自动启动并尝试获取认证信息。

## 贡献

欢迎贡献！请提交拉取请求或报告问题。

## 许可证

此项目使用 MIT 许可证。