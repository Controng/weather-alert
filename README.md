# 浦东新区橙色/红色天气预警邮件周报

数据来源：

https://sh.weather.com.cn/zhyj/index.shtml

程序直接读取网页 DOM：

- `#alarmList li`
- `.alarm-head span`：预警标题
- `.alarm-head em`：发布时间
- `.alarm-body`：详细内容

## 自动运行

- 每小时采集一次预警；
- 每周五上海时间上午 9:00 发送过去 7 天周报；
- 只统计浦东新区的橙色或红色预警；
- 无符合条件记录时，正式周报不发送；
- 手动运行选择 `test`，会发送测试邮件。

## GitHub Secrets

仓库中需要保存：

- `GMAIL_USER`
- `GMAIL_APP_PASSWORD`

收件邮箱固定为：

`june.shao@disney.com`
