"""Local, single-use wallet authorization page. No third-party scripts or key export."""

HTML = '''<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hedge Bot — подключить торговый ключ</title>
<style>body{background:#0f1113;color:#f2f3f5;font:17px system-ui;margin:50px auto;max-width:760px;padding:24px}h1{font-size:28px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#1c2023;padding:20px}button{font:inherit;padding:12px 20px;cursor:pointer}p{line-height:1.6}.muted{color:#a2a9b2}</style>
<h1>Привязать отдельный торговый ключ</h1>
<p>Основной приватный ключ остаётся в вашем кошельке. Здесь подтверждается только привязка отдельного ключа бота.</p>
<p>Ключ Nado имеет полные права на субаккаунт, включая вывод. API-ключ Lighter также имеет более широкие права, чем торговля. Программа не предоставляет команд перевода или вывода.</p>
<pre id="details">Получаю данные запроса…</pre>
<button id="sign" disabled>Подключить кошелёк и подписать привязку</button>
<p id="status" class="muted"></p>
<script>
const token=location.hash.slice(1); history.replaceState(null,'',location.pathname);
const details=document.getElementById('details'), status=document.getElementById('status'), button=document.getElementById('sign');
let challenge;
async function call(path,body){const r=await fetch(path,{method:body?'POST':'GET',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});const v=await r.json();if(!r.ok||!v.ok)throw Error(v.error||'Запрос не выполнен');return v;}
call('/wallet/payload').then(v=>{challenge=v.challenge;details.textContent=JSON.stringify(challenge.display,null,2);button.disabled=false;}).catch(e=>status.textContent=e.message);
button.onclick=async()=>{button.disabled=true;try{
 if(!window.ethereum)throw Error('Откройте эту страницу в браузере с расширением вашего кошелька.');
 const accounts=await window.ethereum.request({method:'eth_requestAccounts'});
 const account=accounts.find(a=>a.toLowerCase()===challenge.owner.toLowerCase());
 if(!account)throw Error('Выберите в кошельке адрес аккаунта, указанный выше.');
 let signature;
 if(challenge.method==='eth_signTypedData_v4'){
   const chain='0x'+Number(challenge.typed_data.domain.chainId).toString(16);
   const current=await window.ethereum.request({method:'eth_chainId'});
   if(current.toLowerCase()!==chain.toLowerCase())await window.ethereum.request({method:'wallet_switchEthereumChain',params:[{chainId:chain}]});
   signature=await window.ethereum.request({method:challenge.method,params:[account,JSON.stringify(challenge.typed_data)]});
 }else if(challenge.method==='personal_sign'){
   const hex='0x'+Array.from(new TextEncoder().encode(challenge.message_utf8),b=>b.toString(16).padStart(2,'0')).join('');
   signature=await window.ethereum.request({method:'personal_sign',params:[hex,account]});
 }else throw Error('Неизвестный тип подписи.');
 status.textContent='Отправляю подпись регистрации…';
 const v=await call('/wallet/complete',{signature});
 status.textContent=v.message||'Регистрация отправлена. Вернитесь в приложение и проверьте ключ.';
 }catch(e){status.textContent=e.message;button.disabled=false;}
};
</script></html>'''
