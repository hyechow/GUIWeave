---
id: context.router.browser.known_apps
source_type: context_block
platform: browser
scope:
  - router
owner: gui_agent.adapters.browser.router_prompt
eval_suites:
  - evals/browser/router
version: 1
---

当前输入、对话历史或用户偏好中明确提到的已知网站：{apps}。这些网站系统已有内置知识（入口地址、访问方式、用法），用户明确提到这些网站名时视为目标明确：直接生成 goal 并原样保留网站名，不要因缺少网址/入口/登录方式而反问。不要把「当前网站」「我们的店铺」「our shop」「后台」等模糊指代猜成清单中的网站名；这类指代只能依据对话历史或用户偏好解析，否则保留原说法或反问。其后的「按 @文件路径」是任务配置引用，原样保留，不要当成要在浏览器中打开的文件。⚠️ 此清单只是补充信息、不是白名单：用户提到清单之外的网站时按上述规则照常处理，绝不因为「不在清单中」而反问或拒绝。
