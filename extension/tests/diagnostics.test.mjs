import test from 'node:test';import assert from 'node:assert/strict';
import {boundedEvent,appendEvent} from '../diagnostics.mjs';
test('diagnostics accepts fixed codes and bounded timings only',()=>{
  assert.equal(boundedEvent('password=secret',1),null);
  assert.deepEqual(boundedEvent('native-error',900000,100),{at:100,code:'native-error',ms:60000});
  assert.deepEqual(appendEvent([{code:'native-error',at:1,url:'https://private/',password:'secret'}],null),[{code:'native-error',at:1}]);
});
test('diagnostics deduplicates storms and caps history at50',()=>{
  let rows=[];for(let i=0;i<100;i++)rows=appendEvent(rows,boundedEvent('native-error',2,i*31000));
  assert.equal(rows.length,50);assert.equal(appendEvent(rows,boundedEvent('native-error',2,99*31000+1)).length,50);
});
