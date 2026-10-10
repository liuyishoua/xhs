"""The Python Creator DS-program field must reach the actual JS signer."""
import json
import subprocess

import pytest


@pytest.mark.parametrize("program_key", ["dsProgram", "dsfProgram"])
def test_creator_signer_executes_server_program_instead_of_static_fallback(program_key):
    script = r"""
const fs = require('fs');
const {signFull} = require('./xhs_utils/xhs_core/js/sign.js');
const {base58CustomDecode,MNS0101_KEYSTREAM} = require('./xhs_utils/xhs_core/js/mns.js');
const key = JSON.parse(fs.readFileSync(0,'utf8')).program_key;
const input = {api:'/web_api/sns/v2/note',data:{title:'test'},
 cookie:'a1=synthetic-device; loadts=1791000000000',
 now:1791000000010,loadts:1791000000000,version:123,seq:1,envConst:1345,
 envFpTail:Array(14).fill(1),deviceTag:'a1',appId:'ugc',platform:'Linux',
 tier:'0101',webBuild:'1.31.0',b1:'',dslPair:'1791000000000;synthetic'};
input[key] = 'function _dsf(unused,bytes){return [1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16]}';
const result = signFull(input);
const encrypted = base58CustomDecode(result.x3.slice(8));
const plain = Buffer.from(encrypted.map((b,i)=>b^MNS0101_KEYSTREAM[i]));
const tail = Array.from(plain.subarray(plain.length-16)).map(b=>b^(input.version&255));
console.log(JSON.stringify(tail));
"""
    result = subprocess.run(["node", "-e", script], input=json.dumps({"program_key": program_key}),
                            text=True, capture_output=True, timeout=10, check=True)
    assert json.loads(result.stdout) == list(range(1, 17))
