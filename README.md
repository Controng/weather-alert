# 浦东新区橙色/红色天气预警邮件周报

这个版本使用 Playwright 浏览器读取上海天气预警网页动态加载的内容。

- 每小时检查一次；
- 只记录浦东新区橙色和红色预警；
- 每周五汇总过去 7 天；
- 手动运行时可以发送测试邮件；
- 收件邮箱：june.shao@disney.com

GitHub Secrets：
- GMAIL_USER
- GMAIL_APP_PASSWORD
