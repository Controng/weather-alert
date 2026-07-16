# 浦东新区橙色/红色天气预警邮件周报

本项目由 GitHub Actions 自动运行：

- 每小时检查上海天气预警官网；
- 仅记录“浦东新区”的橙色和红色预警；
- 每周五（上海时间）汇总过去 7 天记录；
- 有符合条件的预警才发送正式周报；
- 可在 Actions 页面手动运行并发送测试邮件。

数据源：https://sh.weather.com.cn/zhyj/index.shtml

收件邮箱：june.shao@disney.com

## 必需的 GitHub Secrets

- `GMAIL_USER`
- `GMAIL_APP_PASSWORD`

## 手动发送测试邮件

进入仓库的 **Actions** → **Pudong Weather Alert** → **Run workflow** → **Run workflow**。
