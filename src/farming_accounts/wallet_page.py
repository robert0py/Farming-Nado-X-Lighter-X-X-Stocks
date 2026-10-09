"""Single-use wallet authorization. Only injected providers; no external scripts."""

HTML = '''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hedge Bot — привязка торгового ключа</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}body{background:#0f1113;color:#f1f3f5;font:16px/1.55 system-ui;margin:0}
main{max-width:900px;margin:36px auto;padding:0 24px 40px}.brand{font-size:13px;letter-spacing:.14em;color:#a7b1bc}
h1{font-size:30px;line-height:1.2;margin:14px 0}h2{font-size:20px;margin:0 0 12px}p{margin:12px 0}.muted,small{color:#a7b1bc}
.step{border-top:1px solid #31383e;padding:24px 0}.number{color:#8bd7b7;margin-right:12px}
label{display:block;margin:12px 0 6px}input,select{width:100%;background:#191e22;color:#f1f3f5;border:1px solid #46515b;border-radius:5px;padding:11px;font:inherit}
input{font:14px ui-monospace,monospace}button{background:#f1f3f5;color:#15191c;border:0;border-radius:5px;padding:12px 18px;font:600 15px system-ui;cursor:pointer}
button.secondary{color:#f1f3f5;background:#262e34}button:disabled{opacity:.45;cursor:default}button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid #8bd7b7;outline-offset:3px}
.actions{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0}.status{background:#19232a;border-left:3px solid #8bd7b7;padding:15px;overflow-wrap:anywhere}
pre{font:13px/1.6 ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere;background:#171c20;padding:16px;margin:12px 0}
summary{cursor:pointer}details{margin-top:16px}li{margin:8px 0}.notice{font-size:14px;color:#a7b1bc}.success{color:#8bd7b7}
[hidden]{display:none}a{color:#8bd7b7}a.button{display:inline-block;text-decoration:none;background:#262e34;padding:12px 18px;border-radius:5px;font-weight:600}a:focus-visible{outline:2px solid #8bd7b7;outline-offset:3px}
@media(max-width:600px){main{margin-top:24px;padding:0 16px 24px}h1{font-size:25px}.actions button{width:100%}}
</style></head><body><main>
<div class="brand">HEDGE BOT / ТОРГОВЫЙ ДОСТУП</div>
<h1 id="title">Привязать отдельный ключ</h1>
<p class="muted">Ключ бота создан в приложении. Здесь вы подключаете кошелёк аккаунта и подтверждаете его привязку.</p>
<p id="status" class="status" role="status" aria-live="polite">Получаю запрос из приложения…</p>
<section class="step"><h2><span class="number">01</span>Проверьте аккаунт</h2>
<label for="owner">Адрес кошелька, которому принадлежит аккаунт</label><input id="owner" readonly aria-label="Ожидаемый адрес кошелька">
<div class="actions"><button id="copy-owner" class="secondary" disabled>Скопировать адрес</button></div>
<p id="scope" class="notice"></p><details><summary>Данные привязки и права ключа</summary><pre id="details">Загрузка…</pre></details>
<p id="expiry" class="notice"></p></section>
<section class="step"><h2><span class="number">02</span>Подключите этот кошелёк</h2>
<div id="lighter-key-help" hidden>
<h2>Используете Robinhood Wallet?</h2>
<p>Создайте ключ на официальном сайте Lighter, где есть вход через Robinhood Wallet. Вернитесь в бот → «Настроить ключ» → «Robinhood Wallet · API-ключ с сайта Lighter».</p>
<ol><li>Откройте API Keys → Connect Wallet → Robinhood Wallet. Подтвердите подключение на телефоне к адресу из шага 01.</li>
<li>Generate API Key → свободный индекс 4–156 или 158–254; Maker Only выключен. Подтвердите привязку.</li>
<li>Скопируйте Your Account Index, API Key Index и Private Key до закрытия окна создания. Вставьте их в бот и нажмите «Сохранить ключ», затем «Проверить доступ».</li></ol>
<a class="button" href="https://robinhoodchain.lighter.xyz/apikeys" target="_blank" rel="noopener noreferrer">API Keys на официальном сайте Lighter ↗</a>
<p class="notice">Эта страница бота подключает расширения браузера. Robinhood Wallet подключается на сайте Lighter; переносить seed-фразу в MetaMask не нужно. Множитель 2× для сделок бота через API пока не подтверждён.</p>
</div>
<p class="muted">Подключение к сайту Nado или Lighter не даёт доступ этой странице. Разрешите подключение отдельно для адреса бота, показанного в строке браузера.</p>
<label for="provider">Расширение кошелька в этом браузере (MetaMask)</label><select id="provider" disabled><option>Ищу расширения…</option></select>
<div class="actions"><button id="connect" disabled>Подключить кошелёк</button><button id="change" class="secondary" disabled>Выбрать другой аккаунт</button></div>
<label for="account">Адреса, к которым кошелёк разрешил доступ</label><input id="account" readonly value="Кошелёк ещё не подключён">
<p id="match" class="notice"></p></section>
<section class="step"><h2><span class="number">03</span>Подпишите привязку</h2>
<p class="muted">Сверьте адрес и данные запроса в кошельке. После подписи ключ сохранится в приложении автоматически. Затем нажмите «Проверить доступ» для этой площадки.</p>
<div class="actions"><button id="sign" disabled>Подписать привязку ключа</button></div>
<p class="notice">Приватный ключ основного кошелька и seed-фраза остаются в кошельке. Привязка Nado заменяет текущий signer / 1-Click Trading и отменяет resting-ордера. Ключ Nado имеет полные права субаккаунта; ключ Lighter также имеет широкие права. В боте нет команд вывода и перевода.</p></section>
<details class="step" open><summary>Кошелёк не открывается или выбран другой адрес?</summary>
<ol><li>Откройте эту страницу в браузере, где установлен MetaMask, и разблокируйте расширение.</li>
<li>Выберите кошелёк выше и нажмите «Подключить кошелёк». Проверьте значок расширения: запрос может ожидать там.</li>
<li>Если адреса не совпадают, нажмите «Выбрать другой аккаунт» и разрешите доступ адресу из шага 01. Если такого адреса в кошельке нет, выберите нужный кошелёк либо исправьте адрес в приложении.</li>
<li>Если ссылка истекла, создайте новую в приложении. Если подпись уже отправлена, сначала нажмите «Восстановить привязку ключа».</li></ol>
<div class="actions"><button id="rescan" class="secondary">Проверить расширения снова</button><button id="copy-link" class="secondary">Скопировать ссылку для другого браузера</button></div></details>
</main><script>
'use strict';
const $=id=>document.getElementById(id);
const token=location.hash.slice(1), originalURL=location.href;
// Keep the capability in the fragment until completion so reload/copy works.
// Never send it in a URL to the server, store it on disk, or log it.
let challenge, active, accounts=[], busy=false, submitted=false, completed=false;
const providers=[];
const message=text=>{$('status').textContent=text;};
const matches=()=>challenge&&accounts.some(a=>typeof a==='string'&&a.toLowerCase()===challenge.owner.toLowerCase());
const expired=()=>challenge&&challenge.expires_at&&Date.now()/1000>=challenge.expires_at;
function controls(){
 const locked=busy||submitted||completed||expired()||!challenge;
 $('provider').disabled=locked||!providers.length;$('connect').disabled=locked||!active;
 $('change').disabled=locked||!active;$('sign').disabled=locked||!active||!matches();
 $('copy-owner').disabled=!challenge;
 $('account').value=accounts.length?accounts.join(', '):'Кошелёк ещё не подключён';
 $('match').textContent=accounts.length?(matches()?'Адрес совпадает. Можно подписать привязку.':'Адрес не совпадает. Выберите аккаунт с адресом из шага 01.'):'После подключения здесь появится разрешённый адрес.';
}
function addProvider(provider,name){
 if(!provider||typeof provider.request!=='function'||providers.some(p=>p.provider===provider))return;
 const item={provider,name:typeof name==='string'?name.slice(0,100):'EVM-кошелёк'};providers.push(item);
 if(providers.length===1)$('provider').replaceChildren();
 const option=document.createElement('option');option.value=String(providers.length-1);option.textContent=item.name;$('provider').appendChild(option);
 if(!active)selectProvider(0);else controls();
}
function normalizeAccounts(value){return Array.isArray(value)?value.filter(a=>typeof a==='string'&&/^0x[0-9a-fA-F]{40}$/.test(a)):[];}
function accountChanged(value){accounts=normalizeAccounts(value);controls();}
function selectProvider(index){
 if(active&&typeof active.removeListener==='function'){active.removeListener('accountsChanged',accountChanged);active.removeListener('disconnect',disconnected);}
 active=providers[index]?.provider;accounts=[];
 if(active&&typeof active.on==='function'){active.on('accountsChanged',accountChanged);active.on('disconnect',disconnected);}controls();
}
function disconnected(){accounts=[];message('Кошелёк отключён. Разблокируйте его и подключите снова.');controls();}
window.addEventListener('eip6963:announceProvider',event=>addProvider(event.detail?.provider,event.detail?.info?.name));
function discover(){
 window.dispatchEvent(new Event('eip6963:requestProvider'));
 const injected=window.ethereum;
 if(injected){for(const p of injected.providers||[injected])addProvider(p,p.isMetaMask?'MetaMask (injected)':'EVM-кошелёк (injected)');}
 if(!providers.length){$('provider').replaceChildren();const o=document.createElement('option');o.textContent='Расширение кошелька не найдено';$('provider').appendChild(o);
  if(challenge)message(challenge.method==='personal_sign'?'В этом браузере кошелёк не найден. Для Robinhood Wallet откройте API Keys на официальном сайте Lighter по инструкции ниже. Для MetaMask скопируйте эту ссылку в браузер с расширением.':'В этом браузере кошелёк не найден. Скопируйте ссылку и откройте её в браузере с MetaMask. Затем нажмите «Подключить кошелёк».');}controls();
}
function explain(error){
 const code=Number(error?.code);
 if(code===4001)return 'Запрос отклонён в кошельке. Если хотите продолжить, нажмите кнопку ещё раз и подтвердите запрос.';
 if(code===-32002)return 'В кошельке уже есть ожидающий запрос. Откройте расширение и подтвердите или отклоните его. Затем повторите действие.';
 if(code===4100)return 'Кошелёк не разрешил доступ к адресу. Нажмите «Выбрать другой аккаунт» и разрешите адрес из шага 01.';
 if(code===4902)return 'Сеть Nado не добавлена в кошелёк. Добавьте сеть из документации Ink и повторите подпись.';
 return error?.userMessage||'Кошелёк не выполнил запрос. Проверьте расширение, выбранный аккаунт и подключение; затем повторите действие.';
}
function userError(text){const error=new Error(text);error.userMessage=text;return error;}
async function call(path,body){
 const controller=new AbortController(), timer=setTimeout(()=>controller.abort(),70000);
 try{const response=await fetch(path,{method:body?'POST':'GET',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined,signal:controller.signal});
  const value=await response.json();if(!response.ok||!value.ok)throw userError(value.error||'Приложение отклонило запрос.');return value;
 }finally{clearTimeout(timer);}
}
async function connect(change=false){
 if(busy||submitted||!challenge||!active||expired())return;
 const provider=active;busy=true;controls();message('Откройте расширение кошелька и разрешите адрес из шага 01.');
 try{if(change)await provider.request({method:'wallet_requestPermissions',params:[{eth_accounts:{}}]});
  accounts=normalizeAccounts(await provider.request({method:'eth_requestAccounts'}));
  message(matches()?'Кошелёк подключён. Теперь нажмите «Подписать привязку ключа».':'Кошелёк подключён с другим адресом. Нажмите «Выбрать другой аккаунт» и разрешите адрес из шага 01.');
 }catch(error){message(explain(error));}finally{busy=false;controls();}
}
const inkNetworks={
 '0xdef1':{chainId:'0xdef1',chainName:'Ink',nativeCurrency:{name:'Ether',symbol:'ETH',decimals:18},rpcUrls:['https://rpc-gel.inkonchain.com'],blockExplorerUrls:['https://explorer.inkonchain.com']},
 '0xba5ed':{chainId:'0xba5ed',chainName:'Ink Sepolia',nativeCurrency:{name:'Ether',symbol:'ETH',decimals:18},rpcUrls:['https://rpc-gel-sepolia.inkonchain.com'],blockExplorerUrls:['https://explorer-sepolia.inkonchain.com']}
};
async function ensureChain(provider){
 const chain='0x'+BigInt(challenge.typed_data.domain.chainId).toString(16);
 if(String(await provider.request({method:'eth_chainId'})).toLowerCase()!==chain){
  message('Подтвердите переключение на сеть Nado в кошельке.');
  try{await provider.request({method:'wallet_switchEthereumChain',params:[{chainId:chain}]});}
  catch(error){if(Number(error?.code)!==4902||!inkNetworks[chain])throw error;
   await provider.request({method:'wallet_addEthereumChain',params:[inkNetworks[chain]]});
   await provider.request({method:'wallet_switchEthereumChain',params:[{chainId:chain}]});}
 }
 if(String(await provider.request({method:'eth_chainId'})).toLowerCase()!==chain)throw userError('Кошелёк остался в другой сети. Выберите сеть Nado и повторите подпись.');
}
async function sign(){
 if(busy||submitted||!active||!matches()||expired())return;
 const provider=active;busy=true;controls();
 try{
  if(challenge.method==='eth_signTypedData_v4')await ensureChain(provider);
  accounts=normalizeAccounts(await provider.request({method:'eth_accounts'}));
  const account=accounts.find(a=>a.toLowerCase()===challenge.owner.toLowerCase());
  if(!account)throw userError('Адрес в кошельке изменился. Подключите адрес из шага 01 перед подписью.');
  message('Откройте кошелёк, проверьте данные и подтвердите подпись привязки.');
  let signature;
  if(challenge.method==='eth_signTypedData_v4')signature=await provider.request({method:challenge.method,params:[account,JSON.stringify(challenge.typed_data)]});
  else if(challenge.method==='personal_sign'){
   const hex='0x'+Array.from(new TextEncoder().encode(challenge.message_utf8),b=>b.toString(16).padStart(2,'0')).join('');
   signature=await provider.request({method:'personal_sign',params:[hex,account]});
  }else throw userError('Неизвестный тип подписи. Обновите приложение.');
  if(expired())throw userError('Ссылка истекла. Создайте новую сессию привязки в приложении.');
  if(typeof signature!=='string'||!/^0x[0-9a-fA-F]{130}$/.test(signature))throw userError('Кошелёк вернул некорректную подпись.');
  submitted=true;controls();message('Подпись отправляется. Дождитесь результата; повторно подписывать не нужно.');
  const result=await call('/wallet/complete',{signature});completed=true;
  history.replaceState(null,'',location.pathname);$('copy-link').disabled=true;
  message(result.message||'Ключ привязан. Вернитесь в приложение и нажмите «Проверить доступ».');$('status').classList.add('success');
 }catch(error){message(submitted?'Ответ о регистрации не получен или привязка отклонена. Не отправляйте подпись повторно. Вернитесь в приложение и нажмите «Восстановить привязку ключа». '+(error.userMessage||'Проверьте связь с процессом торговли.'):explain(error));}
 finally{busy=false;controls();}
}
async function copy(value){try{await navigator.clipboard.writeText(value);message('Скопировано.');}catch(_){message('Браузер запретил копирование. Выделите адрес или ссылку в адресной строке и скопируйте вручную.');}}
$('connect').onclick=()=>connect();$('change').onclick=()=>connect(true);$('sign').onclick=sign;
$('provider').onchange=()=>{selectProvider(Number($('provider').value));message('Кошелёк выбран. Нажмите «Подключить кошелёк».');};
$('rescan').onclick=discover;$('copy-owner').onclick=()=>copy(challenge.owner);$('copy-link').onclick=()=>copy(originalURL);
async function initialize(){
 if(!token){message('В ссылке нет сессии привязки. Откройте новую ссылку из приложения, а не просто адрес /wallet.');discover();return;}
 try{const value=await call('/wallet/payload');challenge=value.challenge;
  $('owner').value=challenge.owner;$('title').textContent='Привязка ключа · '+(challenge.display?.['Площадка']||'аккаунт');
  $('details').textContent=JSON.stringify(challenge.display,null,2);
  $('lighter-key-help').hidden=challenge.method!=='personal_sign';
  $('scope').textContent=challenge.method==='eth_signTypedData_v4'?'Выберите MetaMask, с которым вы входите в этот субаккаунт Nado. Сеть проверяется перед подписью.':'Для Robinhood Wallet создайте API-ключ на сайте Lighter по инструкции в шаге 02. Для подписи на этой странице используйте MetaMask с адресом аккаунта Lighter на Robinhood Chain.';
  message('Проверьте адрес в шаге 01, затем подключите кошелёк в шаге 02.');discover();
 }catch(error){message(error.userMessage||'Нет связи с приложением. Убедитесь, что процесс торговли включён, и откройте ссылку снова.');}
}
function updateExpiry(){
 if(!challenge?.expires_at||submitted||completed)return;
 const seconds=Math.max(0,Math.ceil(challenge.expires_at-Date.now()/1000));
 $('expiry').textContent='Ссылка действует ещё '+Math.floor(seconds/60)+':'+String(seconds%60).padStart(2,'0');
 if(!seconds){message('Ссылка истекла. Создайте новую сессию привязки в приложении.');controls();}
}
discover();const ready=initialize();setInterval(updateExpiry,1000);
</script></body></html>'''
