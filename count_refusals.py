import json
import sys
sys.stdout.reconfigure(encoding='utf-8')

with open('run_output.json', 'r', encoding='utf-8') as f:
    d = json.load(f)

refusals = 0
for r in d['results']:
    ans = r['answer']
    if ans and ("Khong ho tro" in ans or "het hang" in ans or "Khong tim thay" in ans or "Khong du thong tin" in ans):
        refusals += 1
        print(f"Q: {r['question']}\nA: {ans}\n")

print(f"Total refusals: {refusals}")
