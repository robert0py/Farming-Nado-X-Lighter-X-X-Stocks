// Isolated injected-provider fixtures. No wallets, keys, or exchange endpoints.
const vm = require('node:vm');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const html = fs.readFileSync(0, 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const owner = '0x' + '22'.repeat(20), other = '0x' + '33'.repeat(20);
const signature = '0x' + '11'.repeat(65);
const fixture = {
  owner, method: 'eth_signTypedData_v4', expires_at: Date.now() / 1000 + 300,
  display: {'Площадка': 'Nado'},
  typed_data: {domain: {chainId: 57073}, message: {nonce: '18446744073709551614'}}
};
function wallet(overrides = {}) {
  const calls = [], events = {};
  const provider = {
    isMetaMask: true, calls, chain: '0xdef1', accounts: [owner],
    on(name, fn) {events[name] = fn;}, removeListener(name) {delete events[name];},
    emit(name, value) {events[name]?.(value);},
    async request(value) {
      calls.push(value);
      if (overrides[value.method]) return overrides[value.method](value, provider);
      if (value.method === 'eth_requestAccounts' || value.method === 'eth_accounts') return provider.accounts;
      if (value.method === 'eth_chainId') return provider.chain;
      if (value.method === 'wallet_switchEthereumChain') {provider.chain = value.params[0].chainId; return null;}
      if (value.method === 'wallet_requestPermissions') return [];
      if (value.method === 'eth_signTypedData_v4' || value.method === 'personal_sign') return signature;
      throw Error('Unexpected provider request ' + value.method);
    }
  };
  return provider;
}
async function page({provider, announced = [], challenge = fixture, postError = false, hash = '#session'} = {}) {
  const elements = {}, events = {}, posts = [], copies = [], replacements = [];
  function element(id) {
    return elements[id] ||= {
      textContent: '', value: '', disabled: false, children: [], classList: {add() {}},
      replaceChildren() {this.children = [];}, appendChild(child) {this.children.push(child);}
    };
  }
  const window = {
    ethereum: provider,
    addEventListener(name, fn) {events[name] = fn;},
    dispatchEvent(event) {
      if (event.type === 'eip6963:requestProvider') {
        for (const item of announced) events['eip6963:announceProvider']?.({detail: item});
      }
    }
  };
  const context = vm.createContext({
    window, document: {getElementById: element, createElement: () => element('option-' + Math.random())},
    location: {hash, pathname: '/wallet', href: 'http://127.0.0.1:8765/wallet' + hash},
    history: {replaceState(...args) {replacements.push(args);}},
    navigator: {clipboard: {async writeText(text) {copies.push(text);}}},
    TextEncoder, AbortController, setTimeout, clearTimeout, setInterval: () => 0,
    Event: class {constructor(type) {this.type = type;}},
    async fetch(path, options) {
      assert.equal(options.headers.Authorization, 'Bearer ' + hash.slice(1));
      if (path === '/wallet/payload') return {ok: true, json: async () => ({ok: true, challenge})};
      assert.equal(path, '/wallet/complete');
      posts.push(JSON.parse(options.body));
      if (postError) throw Error('offline');
      return {ok: true, json: async () => ({ok: true, message: 'Привязано'})};
    }
  });
  vm.runInContext(script, context);
  await vm.runInContext('ready', context);
  return {context, elements, posts, copies, replacements, run: code => vm.runInContext(code, context)};
}
async function main() {
  let p = await page();
  assert.match(p.elements.status.textContent, /кошелёк не найден/);
  assert.equal(p.elements.connect.disabled, true);
  await p.elements['copy-link'].onclick();
  assert.match(p.copies[0], /#session$/);
  assert.equal(p.replacements.length, 0);

  let w = wallet(); p = await page({provider: w});
  assert.equal(w.calls.length, 0); // No permission/sign request on page load.
  await p.elements.connect.onclick();
  assert.equal(w.calls.length, 1);
  assert.equal(p.elements.sign.disabled, false);
  assert.equal(p.posts.length, 0);
  await p.elements.sign.onclick();
  const signed = w.calls.find(x => x.method === 'eth_signTypedData_v4');
  assert.equal(JSON.parse(signed.params[1]).message.nonce, fixture.typed_data.message.nonce);
  assert.equal(p.posts.length, 1);
  assert.equal(p.replacements.length, 1);
  await p.run('sign()'); assert.equal(p.posts.length, 1);

  w = wallet(); w.accounts = [other]; p = await page({provider: w});
  await p.elements.connect.onclick();
  assert.equal(p.elements.sign.disabled, true);
  assert.match(p.elements.status.textContent, /другим адресом/);
  await p.run('sign()'); assert.equal(p.posts.length, 0);
  w.accounts = [owner]; await p.elements.change.onclick();
  assert.ok(w.calls.some(x => x.method === 'wallet_requestPermissions'));
  assert.equal(p.elements.sign.disabled, false);
  w.accounts = [other]; await p.elements.sign.onclick();
  assert.equal(p.posts.length, 0);
  assert.ok(!w.calls.some(x => x.method === 'eth_signTypedData_v4'));
  assert.match(p.elements.status.textContent, /Адрес в кошельке изменился/);

  for (const [code, text] of [[4001, /отклонён/], [-32002, /ожидающий запрос/], [4100, /не разрешил/]]) {
    w = wallet({eth_requestAccounts: () => {throw {code};}}); p = await page({provider: w});
    await p.elements.connect.onclick(); assert.match(p.elements.status.textContent, text);
    assert.equal(p.elements.connect.disabled, false); assert.equal(p.posts.length, 0);
  }

  let switched = false;
  w = wallet({wallet_switchEthereumChain: (value, provider) => {
    if (!switched) {switched = true; throw {code: 4902};}
    provider.chain = value.params[0].chainId;
  }, wallet_addEthereumChain: value => {assert.equal(value.params[0].chainId, '0xdef1');}});
  w.chain = '0x1'; p = await page({provider: w});
  await p.elements.connect.onclick(); await p.elements.sign.onclick();
  assert.equal(p.posts.length, 1); assert.ok(w.calls.some(x => x.method === 'wallet_addEthereumChain'));

  w = wallet({wallet_switchEthereumChain: () => {throw {code: 4902};}}); w.chain = '0x1';
  p = await page({provider: w, challenge: {...fixture, typed_data: {domain: {chainId: 999}}}});
  await p.elements.connect.onclick(); await p.elements.sign.onclick();
  assert.equal(p.posts.length, 0); assert.ok(!w.calls.some(x => x.method === 'wallet_addEthereumChain'));

  w = wallet(); p = await page({provider: w, challenge: {...fixture, method: 'personal_sign', message_utf8: 'Lighter 🔑 подпись'}});
  assert.equal(p.elements['lighter-key-help'].hidden, false);
  await p.elements.connect.onclick(); await p.elements.sign.onclick();
  assert.equal(w.calls.find(x => x.method === 'personal_sign').params[0], '0x' + Buffer.from('Lighter 🔑 подпись').toString('hex'));
  assert.ok(!w.calls.some(x => x.method === 'eth_chainId'));

  p = await page({challenge: {...fixture, method: 'personal_sign', message_utf8: 'Lighter'}});
  assert.match(p.elements.status.textContent, /Robinhood Wallet/);
  assert.equal(p.elements['lighter-key-help'].hidden, false);
  assert.equal(p.elements.connect.disabled, true);
  assert.equal(p.posts.length, 0);
  assert.match(html, /href="https:\/\/robinhoodchain\.lighter\.xyz\/apikeys" target="_blank" rel="noopener noreferrer"/);
  p = await page({provider: wallet()});
  assert.equal(p.elements['lighter-key-help'].hidden, true);

  w = wallet(); p = await page({provider: w, postError: true});
  await p.elements.connect.onclick(); await p.elements.sign.onclick(); await p.run('sign()');
  assert.equal(p.posts.length, 1); assert.equal(p.elements.sign.disabled, true);
  assert.match(p.elements.status.textContent, /Восстановить привязку ключа/);
  assert.equal(p.replacements.length, 0);

  const injected = wallet(), announced = wallet();
  p = await page({provider: injected, announced: [{info: {name: '<img onerror=alert(1)>'}, provider: announced}]});
  assert.equal(p.elements.provider.children.length, 2);
  assert.equal(p.elements.provider.children[0].textContent, '<img onerror=alert(1)>');
  await p.elements.connect.onclick(); assert.equal(announced.calls.length, 1); assert.equal(injected.calls.length, 0);
  p.elements.provider.value = '1'; p.elements.provider.onchange();
  assert.equal(p.elements.sign.disabled, true);
  await p.elements.connect.onclick(); assert.equal(injected.calls.length, 1);
  injected.emit('accountsChanged', [other]); assert.equal(p.elements.sign.disabled, true);

  p = await page({provider: wallet(), challenge: {...fixture, expires_at: 1}});
  p.run('updateExpiry()'); assert.equal(p.elements.connect.disabled, true);
  assert.match(p.elements.status.textContent, /истекла/);
  p = await page({hash: ''}); assert.match(p.elements.status.textContent, /нет сессии/);
  console.log('Wallet page: permission, account, provider, chain, signing and recovery cases passed.');
}
main().catch(error => {console.error(error);process.exitCode = 1;});
