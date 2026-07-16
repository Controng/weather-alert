# 浦东新区橙色/红色天气预警邮件周报

数据源：上海天气预警官网：`https://sh.weather.com.cn/zhyj/index.shtml`

功能：
- 每小时用浏览器渲染网页并采集一次；
- 只记录“浦东新区”且等级为“橙色”或“红色”的预警；
- 每周五上海时间上午 9:00 汇总过去 7 天并发送邮件；
- 若一周没有符合条件的预警，正式周报不发送；
- 在 Actions 页面手动运行时，会发送一封测试邮件；
- 测试运行会附带 `weather-page-text` 诊断文件，便于排查网页变化。

需要的 GitHub Secrets：
- `GMAIL_USER`
- `GMAIL_APP_PASSWORD`
