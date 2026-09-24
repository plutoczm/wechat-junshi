"use strict";
const T={
 title:"微信恋爱军师",subtitle:"记住同一个人，不混淆不同会话。",
 unlock:"解锁私人工作台",loginNote:"输入服务器设置的管理令牌，不是 DeepSeek 密钥。刷新后需重新解锁。",
 token:"管理令牌",connect:"解锁",lock:"锁定并清空页面",
 capability:"C 模式已就绪：拟稿、编辑、复制",capabilityNote:"本版不会自动读取手机微信或发消息。B 模式等待免费开源桥接验证。图片不外发；实时梗库尚未接入。",
 people:"人物与微信号",addPerson:"新建人物",personName:"人物代号",create:"创建",group:"合并或拆分多号记忆",groupNote:"选择属于同一个人的微信号，移至目标人物。拆分时先新建人物。历史保留来源，旧草稿会失效。",groupTarget:"目标人物",move:"确认账号归属",
 cloud:"允许本账号的必要文字片段发给 DeepSeek",cloudNote:"同一人的其他号也需分别允许。未允许的内容只在本地查看。已发出的 API 请求不能追回。",saveConsent:"保存数据授权",mode:"回复模式",modeC:"C · 仅生成建议",modeB:"B · 自动发送（未接入）",
 reply:"这一次怎么回",incoming:"粘贴对方的消息",generate:"保存消息并生成建议",draftNote:"草稿不算已发送。手动复制回微信后，再确认实际发送的文字。",
 draftText:"可编辑草稿（用空行分隔多条）",copy:"复制文字",sent:"我已在微信发送这些文字",discard:"丢弃草稿",memory:"共享记忆",search:"搜索历史话题",searchBtn:"查找",searchNote:"检索当前人物的所有已导入账号：关键词匹配 + 最近记录，非全部历史列表。",
 notes:"我确认的重要背景",notesNote:"手动记录偏好、约定和边界。系统会标注为你提供的信息，不会当成独立验证的事实。",addNote:"保存背景",import:"导入已取得的历史文本",importNote:"仅支持 MOBILE.md 定义的 JSON/JSONL，不是微信备份解析器。每次导入一个账号，最多 2000 条 / 2 MiB。",preview:"预览导入",commitImport:"确认导入",export:"导出本账号 JSONL",deleteAccount:"删除本账号及其记忆",footer:"单人私有部署。原始记录保存在运行服务的电脑/服务器，不是 GitHub 或手机浏览器。"
};
const $=id=>document.getElementById(id);
for(const el of document.querySelectorAll("[data-i18n]")) el.textContent=T[el.dataset.i18n];
let token="",people=[],active=null,currentDraft=null,importPreview=null,busy=false,pendingIncoming=null;
const say=message=>{$("notice").textContent=message;};
function node(tag,value,cls){const e=document.createElement(tag);if(value!==undefined)e.textContent=value;if(cls)e.className=cls;return e;}
async function api(path,method="GET",body){
 const response=await fetch(path,{method,headers:{Authorization:"Bearer "+token,...(body?{"Content-Type":"application/json"}:{})},body:body?JSON.stringify(body):undefined,cache:"no-store",credentials:"omit"});
 if(!response.ok){let result={};try{result=await response.json();}catch{}throw new Error(result.detail||"Request failed: "+response.status);}return response.json();
}
function task(id,fn){$(id).addEventListener("click",async()=>{if(busy)return;busy=true;$(id).disabled=true;say("");try{await fn();}catch(e){say(e.message);}finally{busy=false;$(id).disabled=id==="commitImport"&&!importPreview;}});}
function selected(){if(!active)throw new Error("请先选择账号");return active.id;}
function clearDraft(){currentDraft=null;$("draftPanel").hidden=true;$("draftText").value="";}
async function refresh(){
 const state=await api("/api/state");people=state.persons;
 $("keyStatus").textContent=state.api_key_configured?"DeepSeek Flash 密钥已在服务端配置":"提示：服务端尚未设置 DEEPSEEK_API_KEY";
 $("people").replaceChildren();$("groupChoices").replaceChildren();$("groupTarget").replaceChildren();
 for(const p of people){
  const div=node("div",undefined,"person");div.append(node("h3",p.name));
  for(const a of p.accounts){
   const b=node("button",a.label+" · "+a.message_count+" 条","account-button"+(active?.id===a.id?" active":""));
   b.addEventListener("click",async()=>{if(busy)return;busy=true;try{await choose(a);}catch(e){say(e.message);}finally{busy=false;}});div.append(b);
   const label=node("label"),check=document.createElement("input");check.type="checkbox";check.value=a.id;label.append(check,node("span",p.name+" / "+a.label));$("groupChoices").append(label);
  }
  const b=node("button","+ 添加微信号标识","secondary");b.addEventListener("click",async()=>{
   if(busy)return;const label=window.prompt("为该微信号设置唯一标识，例如「她-大号」。这不会读取微信好友列表。");if(!label)return;
   try{await api("/api/accounts","POST",{person_id:p.id,label});await refresh();}catch(e){say(e.message);}});div.append(b);$("people").append(div);
  const option=node("option",p.name);option.value=p.id;$("groupTarget").append(option);
 }
 if(active){active=people.flatMap(p=>p.accounts).find(a=>a.id===active.id)||null;}
 if(active){$("cloud").checked=!!active.cloud;$("accountMeta").textContent=active.message_count+" 条已保存记录";}
}
async function choose(a){active=a;pendingIncoming=null;clearDraft();importPreview=null;$("commitImport").disabled=true;$("importText").value="";$("importPlan").textContent="";$("incoming").value="";$("note").value="";$("search").value="";$("accountHeading").textContent=a.label;$("accountPanel").hidden=false;$("memoryPanel").hidden=false;await refresh();await history();}
function accountLabel(id){return people.flatMap(p=>p.accounts).find(a=>a.id===id)?.label||id;}
async function history(){
 const result=await api("/api/accounts/"+selected()+"/context?q="+encodeURIComponent($("search").value));$("records").replaceChildren();
 for(const r of result.records){const div=node("div",undefined,"record");div.append(node("small",accountLabel(r.account_id)+" / "+({self:"我",friend:"对方",system:"系统"}[r.role]) +" / "+(r.occurred_at||"原始时间未知")));div.append(node("p",r.kind==="sticker"?"[表情占位，未识别] "+r.content:r.content));
  const del=node("button","删除此条","secondary");del.addEventListener("click",async()=>{if(busy||!confirm("删除记录及关联索引和草稿？"))return;try{await api("/api/accounts/"+r.account_id+"/messages/"+r.id,"DELETE");pendingIncoming=null;clearDraft();await refresh();await history();}catch(e){say(e.message);}});div.append(del);$("records").append(div);}
 $("notes").replaceChildren();for(const n of result.notes){const div=node("div",undefined,"record");div.append(node("small",accountLabel(n.account_id)+" / 用户提供"),node("p",n.content));const del=node("button","删除","secondary");del.addEventListener("click",async()=>{if(busy)return;try{await api("/api/accounts/"+n.account_id+"/notes/"+n.id,"DELETE");clearDraft();await history();}catch(e){say(e.message);}});div.append(del);$("notes").append(div);}
}
async function previewRows(rows,batch){return api("/api/accounts/"+selected()+"/import","POST",{rows,batch_id:batch});}
async function commitRows(rows,batch,digest){return api("/api/accounts/"+selected()+"/import","POST",{rows,batch_id:batch,confirm:true,digest});}
task("connect",async()=>{token=$("token").value.trim();try{await refresh();$("login").hidden=true;$("workspace").hidden=false;}catch(e){token="";throw e;}finally{$("token").value="";}});
$("logout").addEventListener("click",()=>{token="";location.reload();});
task("newPerson",async()=>{await api("/api/persons","POST",{name:$("personName").value});$("personName").value="";await refresh();});
task("move",async()=>{const ids=Array.from($("groupChoices").querySelectorAll("input:checked"),e=>e.value);if(!ids.length)throw new Error("请选择账号");if(!confirm(T.groupNote))return;await api("/api/accounts/move","POST",{account_ids:ids,person_id:$("groupTarget").value,confirm_same_person:true});clearDraft();await refresh();if(active)await history();});
task("saveConsent",async()=>{await api("/api/accounts/"+selected(),"PATCH",{cloud:$("cloud").checked,mode:"C"});clearDraft();await refresh();say("授权已更新");});
task("generate",async()=>{
 selected();if(!active.cloud)throw new Error("请先保存当前账号的文字外发授权");
 const content=$("incoming").value.trim();if(!content)throw new Error("请粘贴对方的消息");const rows=[{id:"manual:"+crypto.randomUUID(),role:"friend",content,occurred_at:null}],batch=crypto.randomUUID();
 let saved;if(pendingIncoming?.account===active.id&&pendingIncoming.content===content){saved={message_ids:[pendingIncoming.id]};}else{const plan=await previewRows(rows,batch);saved=await commitRows(rows,batch,plan.digest);pendingIncoming={account:active.id,content,id:saved.message_ids[0]};}clearDraft();
 say("消息已保存，正在生成；不会自动发微信。");
 const d=await api("/api/drafts","POST",{account_id:active.id,message_id:saved.message_ids[0]});currentDraft=d;pendingIncoming=null;$("incoming").value="";
 $("draftText").value=d.messages.join("\n\n");$("explanation").textContent=d.explanation;$("evidence").textContent="检索候选记录："+d.evidence.length+" 条";$("draftPanel").hidden=false;say(d.messages.length?"草稿已生成，请检查再发送":"模型建议本次不回复");await refresh();await history();
});
task("copy",async()=>{await navigator.clipboard.writeText($("draftText").value);say("已复制；请切回正确的微信会话发送");});
task("sent",async()=>{if(!currentDraft)return;if(!confirm("确认你已经在正确的微信会话发送了这些文字？本按钮只记录你的确认，不会发消息。"))return;await api("/api/drafts/"+currentDraft.id,"POST",{action:"confirm_sent",messages:$("draftText").value.split(/\n\s*\n/).map(s=>s.trim()).filter(Boolean)});clearDraft();await refresh();await history();});
task("discard",async()=>{if(currentDraft)await api("/api/drafts/"+currentDraft.id,"POST",{action:"discard"});clearDraft();});
task("searchBtn",history);
task("addNote",async()=>{await api("/api/accounts/"+selected()+"/notes","POST",{content:$("note").value});$("note").value="";clearDraft();await history();});
$("importFile").addEventListener("change",async()=>{const file=$("importFile").files[0];if(!file)return;if(file.size>1500000){say("文件过大，请拆分导入");return;}$("importText").value=await file.text();importPreview=null;$("commitImport").disabled=true;});
$("importText").addEventListener("input",()=>{importPreview=null;$("commitImport").disabled=true;});
task("preview",async()=>{const raw=$("importText").value.trim(),rows=raw.startsWith("[")?JSON.parse(raw):raw.split(/\r?\n/).filter(s=>s.trim()).map(s=>JSON.parse(s));const bytes=new TextEncoder().encode(raw);const hash=await crypto.subtle.digest("SHA-256",bytes),batch=Array.from(new Uint8Array(hash),b=>b.toString(16).padStart(2,"0")).join("");const plan=await previewRows(rows,batch);importPreview={rows,batch,digest:plan.digest,account:active.id};$("importPlan").textContent="共 "+plan.total+" 条，新增 "+plan.new+"，重复 "+plan.duplicates;$("commitImport").disabled=false;});
task("commitImport",async()=>{if(!importPreview||importPreview.account!==selected())throw new Error("请重新预览");await commitRows(importPreview.rows,importPreview.batch,importPreview.digest);importPreview=null;clearDraft();await refresh();await history();say("导入完成");});
task("export",async()=>{const r=await fetch("/api/accounts/"+selected()+"/export",{headers:{Authorization:"Bearer "+token},cache:"no-store",credentials:"omit"});if(!r.ok)throw new Error("Export failed");const url=URL.createObjectURL(await r.blob()),a=document.createElement("a");a.href=url;a.download="messages.jsonl";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
task("deleteAccount",async()=>{if(!confirm("永久删除当前账号的已导入记录、备注和索引？不会删除微信原记录或你的备份。"))return;await api("/api/accounts/"+selected(),"DELETE");active=null;pendingIncoming=null;clearDraft();$("accountPanel").hidden=true;$("memoryPanel").hidden=true;await refresh();});
