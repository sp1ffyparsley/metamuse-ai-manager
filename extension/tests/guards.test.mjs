import {test} from 'node:test';import assert from 'node:assert/strict';
import {sameIncarnation,ruleFor,validateRules,safeCollectionURL,duplicateSuggestions} from '../guards.mjs';
const tab={id:1,url:'https://example.org/apply?cycle=2027',windowId:2,active:false,pinned:false};
test('navigation/reordering/pin/activation/document changes revoke grouping proof',()=>{
 assert.equal(sameIncarnation(tab,{...tab},'doc1','doc1'),true);
 for(const patch of [{url:'https://example.org/other'},{windowId:3},{id:2},{pinned:true},{pendingUrl:'https://example.org/new'},{active:true}])assert.equal(sameIncarnation(tab,{...tab,...patch},'doc1','doc1'),false);
 assert.equal(sameIncarnation(tab,tab,'doc1','doc2'),false);assert.equal(sameIncarnation(tab,tab,null,null),false);
});
test('grouping rules use domain boundaries, deterministic priority, and validated colors',()=>{
 const rules=validateRules([{id:'base',title:'Example',color:'blue',priority:1,domains:['example.org']},{id:'high',title:'Priority',color:'green',priority:9,domains:['example.org'],path:'/apply'}]);
 assert.equal(ruleFor(tab,rules).id,'high');assert.equal(ruleFor({...tab,url:'https://evil-example.org/apply'},rules),null);
 assert.throws(()=>validateRules([{...rules[0],color:'unknown'}]));assert.throws(()=>validateRules([rules[0],rules[0]]));
});
test('collections preserve cycle parameters and refuse challenge or credential URLs',()=>{
 assert.equal(safeCollectionURL(tab.url),true);for(const url of ['file:///etc/passwd','https://u:p@example.org/','https://example.org/?code=123456','https://example.org/?access_token=private'])assert.equal(safeCollectionURL(url),false);
});
test('same URL duplicates are suggestions, never proof that work can be closed',()=>{
 const result=duplicateSuggestions([tab,{...tab,id:2}]);assert.equal(result.length,1);assert.match(result[0].action,/suggest only/);
});
