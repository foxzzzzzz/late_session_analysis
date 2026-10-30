# LLM 401鉴权定位

## 问题与结论

2026-10-30，用户确认.env已更新为新创建的Key。此前验收只保留HTTP状态码，不足以定位。本次捕获HTTP传输层和脱敏服务端错误，未运行交易管线、未改写.env或数据库、未提交。

当前证据表明：**SDK正确读取并发送.env中的Key，官方API服务端明确将该凭据判定为invalid。**原生请求、SDK请求、根路径、/v1路径均一致；不是仅在JSON输出格式、SDK模型前缀转换或/v1拼接下才出现的错误。

不能据此推断该Key在其创建平台必然无效；若来自第三方兼容平台，仍可能是Key与当前api.deepseek.com地址不匹配。Key创建平台/凭据类型尚待确认，也未独立核实服务端账户状态。

## 验收用例与实测

实施前定义：核对URL和Authorization是否使用新.env；对照SDK、原生HTTP、/v1与根路径；保留脱敏正文及request ID。对临时验收脚本增加2项脱敏回归测试：删除原样Key及Bearer凭据，保留服务端错误类型/内容/request ID；无response对象时保留异常body。2项均通过。

| 请求 | 路由 | 结果 | 网络耗时 |
|------|------|------|----------|
| 原生HTTP GET | https://api.deepseek.com/v1/models | 401 | 0.314秒 |
| LiteLLM POST，捕获实际HTTP | https://api.deepseek.com/v1/chat/completions | 401 | 0.157秒 |
| 原生HTTP GET | https://api.deepseek.com/models | 401 | 0.306秒 |
| 原生HTTP POST，最小请求体 | https://api.deepseek.com/chat/completions | 401 | 0.147秒 |

全部无自动重试。两次GET不涉及模型推理；两次POST均使用短诊断输入，不是公告分析或交易请求。

- SDK实际HTTP方法为POST，Authorization为Bearer，凭据值逐字匹配新.env，未导出Authorization头。
- SDK发送的model为`deepseek-v4-flash`，未错误发送`deepseek/deepseek-v4-flash`；完整URL不存在重复/v1。
- 原生最小POST只保留model、messages和max_tokens=64，去掉temperature和response_format，仍得到相同鉴权错误。
- Key为ASCII、无空白、读取值与新.env一致；HTTP_PROXY/HTTPS_PROXY/ALL_PROXY均未设置。未擅自变换Key字符或猜测其他凭据。

## 脱敏服务端错误

四个请求的正文结构一致，以下为原生最小POST的服务端信息（Key按服务端遮蔽格式保留）：

```json
{
  "error": {
    "message": "Authentication Fails, Your api key: ****6f9e is invalid (request_id: c7bf5b2a-750f-4b36-8a01-415b9000b1b7)",
    "type": "authentication_error",
    "param": null,
    "code": "invalid_request_error"
  }
}
```

SDK返回的相应request ID为`03e0d88c-b416-4633-a343-4704f612b265`；SDK将错误包装为BadRequestError，但HTTP状态与服务端错误类型分别为401和authentication_error。不能被异常类名称误导为普通JSON请求体格式问题。

## 官方协议核对与后续

[官方首次调用示例](https://api-docs.deepseek.com/en/)使用`https://api.deepseek.com/chat/completions`与`Authorization: Bearer ...`；当前文档仍接受旧模型名`deepseek-v4-flash`。本次已直接按该HTTP格式对照。

下一步需要确认新Key的创建平台域名，以及复制的是API secret而非Key ID/名称；无需在聊天中发送Key。若确认是DeepSeek官方API secret，使用上述request ID核对服务端认证/账户状态。当前没有足够证据进一步确定“平台不匹配、复制错误、服务端未生效或账户问题”中的哪一项。

完整脱敏HTTP记录：`tmp/llm-401-20261030-181432/diagnosis.json`与`tmp/llm-401-20261030-181515/native_comparison.json`。临时验收脚本已增强，后续失败将保存脱敏error_message、server_error_body及实际请求URL，而非只留状态码。业务客户端代码未修改。

## 19:40复测：原因确认及恢复

用户确认此前API Key复制错误并更正.env。随后使用现有配置单次请求成功，finish_reason=stop，获得完整JSON；不再返回401。这与此前官方服务端明确返回Key invalid的诊断一致，本次无需更改接口地址或请求格式。

原问题原因已确认并解除。调用耗时16.744秒，公告获取4.990秒；输出4条事件，3条严格引用匹配，另1条因跨行空白差异被拦截，语义解释仍有待复核之处。后续属于旁路输出质量问题，详见`tmp/llm-live-20261030-194055/acceptance.md`及[旁路分析](llm-sidecar-analysis.md)。
