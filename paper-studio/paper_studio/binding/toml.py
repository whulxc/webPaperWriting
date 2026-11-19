"""Bounded standard-library TOML writer for Native Runner/registry records."""
import json
import tomllib


def dump_toml(value):
    lines=[]
    def scalar(x):
        if type(x) not in (str,int,float,bool,list):
            raise ValueError('Unsupported TOML scalar')
        return json.dumps(x,ensure_ascii=False,allow_nan=False)
    def table(row,prefix,header=None):
        if header:lines.extend(['',header])
        for key,entry in row.items():
            if not isinstance(entry,dict) and not (isinstance(entry,list) and entry and isinstance(entry[0],dict)):
                lines.append(json.dumps(key)+' = '+scalar(entry))
        for key,entry in row.items():
            name='.'.join(json.dumps(x) for x in (*prefix,key))
            if isinstance(entry,dict):table(entry,(*prefix,key),'['+name+']')
            elif isinstance(entry,list) and entry and isinstance(entry[0],dict):
                if not all(isinstance(x,dict) for x in entry):raise ValueError('Mixed TOML array')
                for item in entry:table(item,(*prefix,key),'[['+name+']]')
    table(value,())
    raw=('\n'.join(lines)+'\n').encode()
    if tomllib.loads(raw.decode())!=value:raise ValueError('TOML roundtrip failed')
    return raw
