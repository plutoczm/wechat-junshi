"use strict";
const T={
 title:"微信恋爱军师",subtitle:"安卓看建议和控制；常驻 Windows 负责微信、记忆与模型。",
 unlock:"解锁私人工作台",loginNote:"输入服务器设置的管理令牌，不是 DeepSeek 密钥。刷新后需重新解锁。",
 token:"管理令牌",connect:"解锁",lock:"锁定并清空页面",refresh:"刷新状态",
 capability:"手机控制台",capabilityNote:"C 模式只给建议；B 模式只对你明确绑定并启用的好友自动发送。完整聊天记忆按人物共享，收发状态按微信号隔离。",
 inbox:"待处理与最近回复",inboxNote:"自动监听到的新文字、C 草稿和 B 发送状态都会出现在这里。“发送结果未知”不会自动重试。",
 people:"人物与微信号",addPerson:"新建人物",personName:"人物代号",create:"创建",
 group:"合并或拆分多号记忆",groupNote:"只勾选确实属于同一个人的微信号。共享的是人物记忆，不共享消息游标或发送队列。拆分时先新建人物。",
 groupTarget:"目标人物",move:"确认账号归属",
 wechatLink:"微信绑定",contactQuery:"搜索当前 Windows 微信好友（备注、昵称或微信号）",searchContact:"搜索好友",
 unlinkWechat:"解除微信绑定",syncHistory:"同步该微信号完整电脑历史",
 cloud:"允许本账号的必要文字片段发给 DeepSeek",cloudNote:"同一人的其他号也需分别授权。表情/图片不会发送给模型。",
 mode:"回复模式",modeOff:"OFF · 只保存不生成",modeC:"C · 自动生成建议，不自动发送",modeB:"B · 自动生成并发送",
 accountEnabled:"启用该微信号的后台监听",saveSettings:"保存模式与授权",
 manualReply:"手动生成一条建议",incoming:"粘贴对方的消息",generate:"保存消息并生成建议",
 draftNote:"这条入口始终是人工控制：生成后不会自动发送。B 模式的自动发送只处理后台监听到的新消息。",
 draftText:"可编辑草稿（用空行分隔多条）",copy:"复制文字",sent:"我已在微信手动发送这些文字",discard:"丢弃草稿",
 memory:"共享长期记忆",search:"搜索历史话题",searchBtn:"查找",
 searchNote:"检索当前人物绑定的所有微信号：相关历史 + 最近记录。原始消息仍保留来源账号。",
 notes:"我确认的重要背景",notesNote:"手动记录偏好、约定和边界；它被标记为你提供的信息，而不是独立验证的事实。",
 addNote:"保存背景",import:"导入其他已取得的历史文本",
 importNote:"微信桥接可直接同步桌面历史；这里保留 JSON/JSONL 手工导入兜底。每次导入一个账号。",
 preview:"预览导入",commitImport:"确认导入",export:"导出本账号 JSONL",deleteAccount:"删除本账号及其记忆",
 footer:"单人私有部署。真实聊天保存在你运行服务的电脑上；GitHub 仓库不保存聊天、密钥或微信数据库。"
};
const $=id=>document.getElementById(id);
for(const el of document.querySelectorAll("[data-i18n]")) el.textContent=T[el.dataset.i18n];

let token="",people=[],state=null,active=null,currentDraft=null,importPreview=null,busy=false,pendingIncoming=null,syncTimer=null;
const say=message=>{$("notice").textContent=message||"";};
function node(tag,value,cls){const e=document.createElement(tag);if(value!==undefined)e.textContent=value;if(cls)e.className=cls;return e;}
function accountById(id){return people.flatMap(p=>p.accounts).find(a=>a.id===id)||null;}
function personByAccount(id){return people.find(p=>p.accounts.some(a=>a.id===id))||null;}
function accountLabel(id){return accountById(id)?.label||id;}
async function api(path,method="GET",body){
 const response=await fetch(path,{method,headers:{Authorization:"Bearer "+token,...(body?{"Content-Type":"application/json"}:{})},body:body?JSON.stringify(body):undefined,cache:"no-store",credentials:"omit"});
 if(!response.ok){let result={};try{result=await response.json();}catch{}throw new Error(result.detail||"Request failed: "+response.status);}
 return response.json();
}
function task(id,fn){$(id).addEventListener("click",async()=>{if(busy)return;busy=true;$(id).disabled=true;say("");try{await fn();}catch(e){say(e.message);}finally{busy=false;$(id).disabled=id==="commitImport"&&!importPreview;}});}
function selected(){if(!active)throw new Error("请先选择账号");return active.id;}
function clearDraft(){currentDraft=null;$("draftPanel").hidden=true;$("draftText").value="";$("explanation").textContent="";$("evidence").textContent="";}
function bridgeText(b){
 if(!b?.enabled)return "微信桥接：当前主机不是 Windows，只有手机工作台可用";
 if(!b.installed)return "微信桥接：未安装 requirements-wechat.txt";
 if(!b.connected)return "微信桥接：未连接（请确认 Windows 微信已登录）";
 return "微信桥接：已连接 · wechatauto-replica "+(b.package_version||"")+" · 目标微信 4.1.13.65";
}
function renderStatus(){
 $("keyStatus").textContent=state.api_key_configured?"DeepSeek Flash：已配置":"DeepSeek Flash：未配置 DEEPSEEK_API_KEY";
 $("bridgeStatus").textContent=bridgeText(state.transport);
 const t=state.trends||{};
 $("trendStatus").textContent=t.enabled?("公开热梗/热榜："+(t.last_refresh?"已更新 "+t.last_refresh:"等待后台首次更新")):"公开热梗/热榜：已关闭";
}
function renderPeople(){
 $("people").replaceChildren();$("groupChoices").replaceChildren();$("groupTarget").replaceChildren();
 for(const p of people){
  const div=node("div",undefined,"person");div.append(node("h3",p.name));
  for(const a of p.accounts){
   const suffix=a.external_id?(" · 微信已绑定"+(a.mode?" · "+a.mode:"")):" · 未绑定微信";
   const b=node("button",a.label+" · "+a.message_count+" 条"+suffix,"account-button"+(active?.id===a.id?" active":""));
   b.addEventListener("click",async()=>{if(busy)return;busy=true;try{await choose(a.id);}catch(e){say(e.message);}finally{busy=false;}});div.append(b);
   const label=node("label"),check=document.createElement("input");check.type="checkbox";check.value=a.id;label.append(check,node("span",p.name+" / "+a.label));$("groupChoices").append(label);
  }
  const b=node("button","+ 添加微信号槽位","secondary");b.addEventListener("click",async()=>{
   if(busy)return;const label=window.prompt("给这个微信号槽位起一个唯一名字，例如「她-大号」。之后可搜索并绑定真实微信好友。");if(!label)return;
   try{await api("/api/accounts","POST",{person_id:p.id,label});await refresh();}catch(e){say(e.message);}
  });div.append(b);$("people").append(div);
  const option=node("option",p.name);option.value=p.id;$("groupTarget").append(option);
 }
}
function renderActive(){
 if(!active){$("accountPanel").hidden=true;$("memoryPanel").hidden=true;return;}
 $("accountPanel").hidden=false;$("memoryPanel").hidden=false;
 $("accountHeading").textContent=active.label;
 $("accountMeta").textContent=active.message_count+" 条已保存记录"+(active.external_id?" · "+active.external_id:"");
 $("cloud").checked=!!active.cloud;
 $("mode").value=active.mode||"C";
 $("accountEnabled").checked=active.enabled===null||active.enabled===undefined?true:!!active.enabled;
 const bound=!!active.external_id;
 $("bindingStatus").textContent=bound?("已绑定："+active.display_name+" · "+active.external_id+" · 上次历史同步 "+(active.last_sync_at||"未完成")):"尚未绑定真实微信好友。先在 Windows 微信登录后搜索并绑定。";
 $("unlinkWechat").hidden=!bound;$("syncHistory").hidden=!bound;
 if(!bound)$("syncStatus").textContent="";
}
function groupInbox(items){
 const map=new Map();
 for(const item of items||[]){
  const key=item.draft_id||("msg:"+item.id);
  if(!map.has(key))map.set(key,{...item,contents:[]});
  map.get(key).contents.push(item.content);
 }
 return Array.from(map.values());
}
function renderInbox(items){
 $("inbox").replaceChildren();
 const groups=groupInbox(items);
 if(!groups.length){$("inbox").append(node("p","暂无待处理消息。","muted"));return;}
 for(const item of groups){
  const card=node("div",undefined,"inbox-card");
  card.append(node("small",(item.person_name||"人物")+" / "+(item.label||accountLabel(item.account_id))+" · "+item.status));
  for(const content of item.contents)card.append(node("p","对方："+content));
  if(item.draft_status==="skipped")card.append(node("p","建议：本次不回复。","ok"));
  if(item.draft_messages?.length){
   const draft=node("div",undefined,"draft-preview");for(const m of item.draft_messages)draft.append(node("p","建议："+m));card.append(draft);
   if(item.explanation)card.append(node("small","提示："+item.explanation));
   const copy=node("button","复制建议","secondary");copy.addEventListener("click",async()=>{try{await navigator.clipboard.writeText(item.draft_messages.join("\n\n"));say("已复制建议");}catch(e){say("复制失败，请手动选择文字");}});card.append(copy);
   if(item.draft_status==="pending"){
    const sent=node("button","确认我已手动发送","secondary");sent.addEventListener("click",async()=>{if(busy||!confirm("确认这些文字已经由你手动发到正确的微信会话？"))return;busy=true;try{await api("/api/drafts/"+item.draft_id,"POST",{action:"confirm_sent",messages:item.draft_messages});await refresh();}catch(e){say(e.message);}finally{busy=false;}});card.append(sent);
    const discard=node("button","丢弃","secondary");discard.addEventListener("click",async()=>{if(busy)return;busy=true;try{await api("/api/drafts/"+item.draft_id,"POST",{action:"discard"});await refresh();}catch(e){say(e.message);}finally{busy=false;}});card.append(discard);
   }
  }
  if(item.outbox_state==="failed"&&item.outbox_id){
   const retry=node("button","确认安全后重试发送","danger");retry.addEventListener("click",async()=>{if(busy||!confirm("只有明确“未发送”的失败才能重试。继续？"))return;busy=true;try{await api("/api/outbox/"+item.outbox_id+"/retry","POST",{});await refresh();}catch(e){say(e.message);}finally{busy=false;}});card.append(retry);
  }
  if(item.outbox_state==="unknown")card.append(node("p","发送结果未知：系统不会自动重试，请先在微信里核对。","warning"));
  if(item.outbox_state==="confirmed")card.append(node("p","B 模式：已从微信本地数据库回读确认发送。","ok"));
  $("inbox").append(card);
 }
}
async function refresh(renderMemory=false){
 state=await api("/api/state");people=state.persons;
 const activeId=active?.id;active=activeId?accountById(activeId):null;
 renderStatus();renderPeople();renderActive();renderInbox(state.inbox);
 if(renderMemory&&active)await history();
}
async function choose(id){
 active=accountById(id);pendingIncoming=null;clearDraft();importPreview=null;$("commitImport").disabled=true;
 $("importText").value="";$("importPlan").textContent="";$("incoming").value="";$("note").value="";$("search").value="";
 renderActive();renderPeople();$("contactResults").replaceChildren();await history();await pollSyncOnce();
}
async function history(){
 const result=await api("/api/accounts/"+selected()+"/context?q="+encodeURIComponent($("search").value));$("records").replaceChildren();
 for(const r of result.records){
  const div=node("div",undefined,"record");div.append(node("small",accountLabel(r.account_id)+" / "+({self:"我",friend:"对方",system:"系统"}[r.role]||r.role)+" / "+(r.occurred_at||"原始时间未知")));
  div.append(node("p",r.kind==="sticker"?"[表情占位，未识别] "+r.content:r.content));
  const del=node("button","删除此条","secondary");del.addEventListener("click",async()=>{if(busy||!confirm("删除记录及关联索引和草稿？"))return;busy=true;try{await api("/api/accounts/"+r.account_id+"/messages/"+r.id,"DELETE");pendingIncoming=null;clearDraft();await refresh();await history();}catch(e){say(e.message);}finally{busy=false;}});div.append(del);$("records").append(div);
 }
 $("notes").replaceChildren();for(const n of result.notes){const div=node("div",undefined,"record");div.append(node("small",accountLabel(n.account_id)+" / 用户提供"),node("p",n.content));const del=node("button","删除","secondary");del.addEventListener("click",async()=>{if(busy)return;busy=true;try{await api("/api/accounts/"+n.account_id+"/notes/"+n.id,"DELETE");clearDraft();await history();}catch(e){say(e.message);}finally{busy=false;}});div.append(del);$("notes").append(div);}
}
async function previewRows(rows,batch){return api("/api/accounts/"+selected()+"/import","POST",{rows,batch_id:batch});}
async function commitRows(rows,batch,digest){return api("/api/accounts/"+selected()+"/import","POST",{rows,batch_id:batch,confirm:true,digest});}
async function pollSyncOnce(){
 if(!active?.external_id)return;
 const s=await api("/api/accounts/"+active.id+"/sync");
 if(s.state==="idle"){$("syncStatus").textContent="";return;}
 $("syncStatus").textContent=s.state==="running"?("历史同步中："+s.imported+"/"+s.total):s.state==="done"?("历史同步完成：新增 "+s.imported+" 条"):("历史同步失败："+(s.error||"未知错误"));
 if(s.state==="running"){
  clearTimeout(syncTimer);syncTimer=setTimeout(async()=>{if(token&&active){try{await pollSyncOnce();}catch(e){say(e.message);}}},1500);
 }else if(s.state==="done"){await refresh();await history();}
}

task("connect",async()=>{token=$("token").value.trim();try{await refresh();$("login").hidden=true;$("workspace").hidden=false;}catch(e){token="";throw e;}finally{$("token").value="";}});
$("logout").addEventListener("click",()=>{token="";location.reload();});
task("refreshAll",async()=>{await refresh(true);say("状态已刷新");});
task("newPerson",async()=>{await api("/api/persons","POST",{name:$("personName").value});$("personName").value="";await refresh();});
task("move",async()=>{const ids=Array.from($("groupChoices").querySelectorAll("input:checked"),e=>e.value);if(!ids.length)throw new Error("请选择账号");if(!confirm(T.groupNote))return;await api("/api/accounts/move","POST",{account_ids:ids,person_id:$("groupTarget").value,confirm_same_person:true});clearDraft();await refresh();if(active)await history();});

task("searchContact",async()=>{
 if(!state.transport?.connected)throw new Error("Windows 微信桥接尚未连接");
 const q=$("contactQuery").value.trim();if(!q)throw new Error("请输入好友备注、昵称或微信号");
 const result=await api("/api/bridge/contacts?q="+encodeURIComponent(q));$("contactResults").replaceChildren();
 if(!result.contacts.length){$("contactResults").append(node("p","没有找到匹配的私聊好友。","muted"));return;}
 for(const c of result.contacts){
  const row=node("div",undefined,"contact-row");row.append(node("span",c.display_name+" · "+c.external_id));
  const b=node("button","绑定","secondary");b.addEventListener("click",async()=>{if(busy||!active)return;if(!confirm("把当前账号槽位绑定到微信好友「"+c.display_name+"」？"))return;busy=true;try{await api("/api/accounts/"+active.id+"/link-wechat","POST",{external_id:c.external_id,display_name:c.display_name,owner_external_id:c.owner_external_id});await refresh(true);$("contactResults").replaceChildren();say("微信好友已绑定");}catch(e){say(e.message);}finally{busy=false;}});row.append(b);$("contactResults").append(row);
 }
});
task("unlinkWechat",async()=>{if(!active||!confirm("解除绑定？本地长期记忆不会删除，但后台监听和 B 自动发送会停止。"))return;await api("/api/accounts/"+active.id+"/link-wechat","DELETE");await refresh(true);});
task("syncHistory",async()=>{if(!active)throw new Error("请选择账号");await api("/api/accounts/"+active.id+"/sync","POST",{});say("已开始在 Windows 端同步完整本地历史");await pollSyncOnce();});
task("saveConsent",async()=>{await api("/api/accounts/"+selected(),"PATCH",{cloud:$("cloud").checked,mode:$("mode").value,enabled:$("accountEnabled").checked});clearDraft();await refresh();say("模式与授权已更新");});

task("generate",async()=>{
 selected();if(!active.cloud)throw new Error("请先保存当前账号的文字外发授权");
 const content=$("incoming").value.trim();if(!content)throw new Error("请粘贴对方的消息");
 const rows=[{id:"manual:"+crypto.randomUUID(),role:"friend",content,occurred_at:null}],batch=crypto.randomUUID();
 let saved;if(pendingIncoming?.account===active.id&&pendingIncoming.content===content){saved={message_ids:[pendingIncoming.id]};}else{const plan=await previewRows(rows,batch);saved=await commitRows(rows,batch,plan.digest);pendingIncoming={account:active.id,content,id:saved.message_ids[0]};}
 clearDraft();say("消息已保存，正在生成；这个手动入口不会自动发微信。");
 const d=await api("/api/drafts","POST",{account_id:active.id,message_id:saved.message_ids[0]});currentDraft=d;pendingIncoming=null;$("incoming").value="";
 $("draftText").value=d.messages.join("\n\n");$("explanation").textContent=d.explanation;$("evidence").textContent="检索候选记录："+d.evidence.length+" 条";$("draftPanel").hidden=false;
 say(d.messages.length?"草稿已生成，请检查再发送":"模型建议本次不回复");await refresh();await history();
});
task("copy",async()=>{await navigator.clipboard.writeText($("draftText").value);say("已复制；请切回正确的微信会话发送");});
task("sent",async()=>{if(!currentDraft)return;if(!confirm("确认你已经在正确的微信会话发送了这些文字？"))return;await api("/api/drafts/"+currentDraft.id,"POST",{action:"confirm_sent",messages:$("draftText").value.split(/\n\s*\n/).map(s=>s.trim()).filter(Boolean)});clearDraft();await refresh();await history();});
task("discard",async()=>{if(currentDraft)await api("/api/drafts/"+currentDraft.id,"POST",{action:"discard"});clearDraft();await refresh();});
task("searchBtn",history);
task("addNote",async()=>{await api("/api/accounts/"+selected()+"/notes","POST",{content:$("note").value});$("note").value="";clearDraft();await refresh();await history();});
$("importFile").addEventListener("change",async()=>{const file=$("importFile").files[0];if(!file)return;if(file.size>3000000){say("文件过大，请拆分导入");return;}$("importText").value=await file.text();importPreview=null;$("commitImport").disabled=true;});
$("importText").addEventListener("input",()=>{importPreview=null;$("commitImport").disabled=true;});
task("preview",async()=>{const raw=$("importText").value.trim(),rows=raw.startsWith("[")?JSON.parse(raw):raw.split(/\r?\n/).filter(s=>s.trim()).map(s=>JSON.parse(s));const bytes=new TextEncoder().encode(raw);const hash=await crypto.subtle.digest("SHA-256",bytes),batch=Array.from(new Uint8Array(hash),b=>b.toString(16).padStart(2,"0")).join("");const plan=await previewRows(rows,batch);importPreview={rows,batch,digest:plan.digest,account:active.id};$("importPlan").textContent="共 "+plan.total+" 条，新增 "+plan.new+"，重复 "+plan.duplicates;$("commitImport").disabled=false;});
task("commitImport",async()=>{if(!importPreview||importPreview.account!==selected())throw new Error("请重新预览");await commitRows(importPreview.rows,importPreview.batch,importPreview.digest);importPreview=null;clearDraft();await refresh();await history();say("导入完成");});
task("export",async()=>{const r=await fetch("/api/accounts/"+selected()+"/export",{headers:{Authorization:"Bearer "+token},cache:"no-store",credentials:"omit"});if(!r.ok)throw new Error("Export failed");const url=URL.createObjectURL(await r.blob()),a=document.createElement("a");a.href=url;a.download="messages.jsonl";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
task("deleteAccount",async()=>{if(!confirm("永久删除当前账号的本地记录、备注、微信绑定与发送队列？不会删除微信原记录或你的备份。"))return;await api("/api/accounts/"+selected(),"DELETE");active=null;pendingIncoming=null;clearDraft();$("accountPanel").hidden=true;$("memoryPanel").hidden=true;await refresh();});

setInterval(async()=>{if(token&&!busy){try{await refresh(false);}catch(e){say("自动刷新失败："+e.message);}}},8000);
