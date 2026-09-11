const {test}=require('node:test');
const assert=require('node:assert/strict');
const crypto=require('node:crypto');
const fs=require('node:fs');
const workflow=JSON.parse(fs.readFileSync('workflows/n8n_envio_seguro.json','utf8'));
const node=name=>workflow.nodes.find(n=>n.name===name);
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
const run=(name,input,source=input,buffer=Buffer.from('%PDF-1.4\n%%EOF'))=>
 new AsyncFunction('$input','$','require',node(name).parameters.jsCode).call(
 {helpers:{getBinaryDataBuffer:async()=>buffer}}, {first:()=>input},()=>({first:()=>source}), require);
const pdf=Buffer.from('%PDF-1.4\n%%EOF');
function request(){return {json:{headers:{'x-workflow-token':'secret'},body:{protocol_version:'1',
 email:'a@example.com',partido:'Atleti vs Rival',partido_id:'1',tipo:'entrada',tipo_recurso:'abono',
 recurso_id:'1',idempotency_key:'a'.repeat(64),pdf_sha256:crypto.createHash('sha256').update(pdf).digest('hex'),pdf_bytes:String(pdf.length)}},
 binary:{pdf:{fileName:'entrada.pdf',mimeType:'application/pdf'}}};}
test('webhook autenticado, sin datos guardados, sin credenciales exportadas',()=>{
 assert.equal(node('Webhook').parameters.authentication,'headerAuth');
 assert.equal(workflow.active,false);
 assert.equal(workflow.settings.saveDataSuccessExecution,'none');
 assert.equal(workflow.settings.saveDataErrorExecution,'none');
 assert.equal(workflow.settings.saveManualExecutions,false);
 assert.equal(node('Enviar correo').retryOnFail,false);
 assert(workflow.nodes.every(n=>!n.credentials));
});
test('validación acepta PDF correcto y elimina cabeceras',async()=>{
 const [r]=await run('Validar solicitud',request());
 assert.equal(r.json.valid,true);assert.equal(r.json.headers,undefined);assert(r.binary.pdf);
});
test('rechaza destinatarios múltiples, tipo desconocido y hash alterado',async()=>{
 for(const changes of [{email:'a@example.com,b@example.com'},{tipo:'otro'},{pdf_sha256:'c'.repeat(64)},{partido:'a\r\nb'}]){
 const r=request();Object.assign(r.json.body,changes);
 assert.equal((await run('Validar solicitud',r))[0].json.valid,false);
 }
 const r=request();delete r.binary;
 assert.equal((await run('Validar solicitud',r))[0].json.valid,false);
});
test('reserva ajena nunca envía, enviado devuelve acuse y conflicto bloquea',async()=>{
 const [source]=await run('Validar solicitud',request());
 const receipt={state:'processing',payload_hash:source.json.payload_hash,owner_token:source.json.owner_token};
 assert.equal((await run('Decidir envio',{json:receipt},source))[0].json.send,true);
 const other=(await run('Decidir envio',{json:{...receipt,owner_token:'other'}},source))[0];
 assert.equal(other.json.http_status,409);assert.equal(other.binary,undefined);
 const sent=(await run('Decidir envio',{json:{...receipt,state:'sent'}},source))[0];
 assert.equal(sent.json.reply.ok,true);assert.equal(sent.json.send,false);
 const conflict=(await run('Decidir envio',{json:{...receipt,state:'sent',payload_hash:'other'}},source))[0];
 assert.equal(conflict.json.http_status,409);
});
test('SMTP solo se confirma para el destinatario aceptado',async()=>{
 const source=request();
 await assert.rejects(run('Comprobar SMTP',{json:{accepted:[]}},source));
 await assert.rejects(run('Comprobar SMTP',{json:{accepted:['other@example.com']}},source));
 assert.equal((await run('Comprobar SMTP',{json:{accepted:['a@example.com'],rejected:[]}},source))[0].json.confirmed,true);
});
