# 美国 HDMI / Pro AV 首触开发信模板（英文）

> 用途：对 `leads_us_hdmi_outreach.csv` 里的高/中优先级公司做冷启动开发。
> 变量用 `{{ }}` 占位，发送前替换为真实信息。SMTP 当前未配置（EMAIL_SMTP_HOST 为空），本模板仅作草稿，不自动发送。

## 主题行（二选一）
- `OEM/ODM partner for HDMI extenders & 4K/8K matrix switchers — {{our_company}}`
- `Expand your AV SKUs with mix-and-match HDMI extenders (TX/RX) — {{our_company}}`

## 正文
Hi {{contact_person|there}},

I'm {{our_name}} from {{our_company}}, a manufacturer of Pro AV signal-distribution
products — HDMI 2.1 extenders, 4K60 / 8K HDMI matrix switchers, HDBaseT and
AV-over-IP. We supply OEM / ODM and private-label to AV integrators and brand
owners across the US.

I came across {{company_name}} and your work in {{industry_short}}. We help partners
like you:

- Expand SKUs with mix-and-match TX/RX HDMI extenders (up to 4K60 / 8K)
- Modular matrix switchers — HDBaseT, KVM, and AV-over-IP builds
- HDCP 2.2 / HDR compliant, with fast lead times and US-friendly MOQs

Happy to send a catalog or spec sheet. Would a 15-minute call next week make sense?

Best regards,
{{our_name}}
{{our_company}} | {{our_email}} | {{our_phone}}

## 发送建议
- 优先用 `primary_email`（sales@ / info@ 优先，其次首个邮箱）。
- 首批只发 **high** 优先级（37 家），收到回复再扩到 medium。
- 同公司多邮箱不要群发，避免被标记 spam；一封致主联系人即可。
- 配 `EMAIL_REQUIRE_APPROVAL_BEFORE_SEND=true`（当前已开）可先人工过目再发。
