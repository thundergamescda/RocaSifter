'use strict';
let csrfToken='';
const byId=id=>document.getElementById(id);
async function api(path,data){
 const options={credentials:'same-origin',headers:{}};
 if(data!==undefined){options.method='POST';options.headers['Content-Type']='application/json';options.headers['X-SS-CSRF']=csrfToken;options.body=JSON.stringify(data)}
 const response=await fetch(path,options);const result=await response.json();
 if(!response.ok)throw new Error(result.error||'The service could not complete the request.');
 return result;
}
async function workspace(){
 const me=await api('/api/me');csrfToken=me.csrf_token;
 byId('storeName').textContent=me.store;byId('signin').hidden=true;byId('workspace').hidden=false;
 const square=await api('/api/square/status');
 byId('squareStatus').textContent=square.connected?'Connected to Square '+square.environment+'. Checkout is still in pilot testing.':square.oauth_configured?'Authorize this store to connect its Square account.':'Square application setup is required before store authorization can begin.';
 byId('connectSquare').disabled=!square.oauth_configured;
 byId('connectSquare').textContent=square.connected?'Reconnect Square':'Connect Square';
}
byId('loginForm').onsubmit=async event=>{
 event.preventDefault();const button=event.target.querySelector('button');button.disabled=true;byId('loginStatus').textContent='';
 try{const result=await api('/api/auth/login',{store_slug:byId('storeSlug').value,email:byId('email').value,password:byId('password').value});csrfToken=result.csrf_token;byId('password').value='';await workspace()}
 catch(error){byId('loginStatus').textContent=error.message}finally{button.disabled=false}
};
byId('connectSquare').onclick=async()=>{
 try{const result=await api('/api/square/connect',{});const url=new URL(result.authorization_url);if(!['connect.squareup.com','connect.squareupsandbox.com'].includes(url.hostname)||url.protocol!=='https:')throw Error('Authorization destination was rejected.');window.location.assign(url.href)}
 catch(error){byId('workspaceStatus').textContent=error.message}
};
byId('signOut').onclick=async()=>{try{await api('/api/auth/logout',{});csrfToken='';byId('workspace').hidden=true;byId('signin').hidden=false}catch(error){byId('workspaceStatus').textContent=error.message}};
workspace().catch(()=>{});
