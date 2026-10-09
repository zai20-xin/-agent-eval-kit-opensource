import sys
lines = [f"line {i}: INFO ok" if i % 10 else f"line {i}: ERROR bad" for i in range(600)]
off = int(sys.argv[1]) if len(sys.argv) > 1 else 0
print("\n".join(lines[off:off+50]))
print(f"__HAS_MORE__ total=600 returned_from={off}")
