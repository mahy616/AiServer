"""Frozen, deterministic pure-computation tasks and independent numeric oracles."""
from dataclasses import dataclass
import json
import math
import random


@dataclass(frozen=True)
class Case:
    id: str
    level: str
    kind: str
    cap: int
    title: str
    specification: str
    source: str
    reference: str
    tests: tuple
    expected: tuple
    sample: tuple = ()
    sample_result: tuple = ()
    faulty: tuple = ()


SKELETON = """using System;
using System.Collections.Generic;
using System.Linq;
public static class Script
{
    public static double[] Run(double[] input)
    {
        throw new NotImplementedException();
    }
}
"""


def transform(x, channel, bad=False, channels=1):
    lower, upper = -20-channel%4, 45+channel%13
    pivot = (channel%9+1)*3
    left, right = (channel%7+1)/10, (channel%5+2)/10
    x = min(upper, max(lower, x))
    if x <= pivot:
        return x*left + channel%3 + (1 if bad and channel == 0 else 0)
    if bad and channel == channels-1:
        right += .1
    return pivot*left + (x-pivot)*right + channel%3


def calibration(data, channels, hard, bad=False):
    groups = {}
    for i in range(0, len(data)-2, 3):
        key, x, w = data[i:i+3]
        if key != int(key) or key < 0 or key >= channels or w <= 0:
            continue
        groups.setdefault(int(key), []).append((transform(x, int(key), bad, channels), w))
    result = []
    for key, items in sorted(groups.items()):
        weight = math.fsum(w for _, w in items)
        mean = math.fsum(x*w for x, w in items)/weight
        result += [key, mean]
        if hard:
            variance = (math.fsum(x*x*w for x, w in items)/weight + mean*mean if bad else
                        math.fsum(w*(x-mean)**2 for x, w in items)/weight)
            result += [math.sqrt(max(0, variance))]
        result += [len(items)]
    return result


def calibration_source(channels, hard, bad):
    source = """using System;
using System.Collections.Generic;
public static class Script
{
    // Contract: groups sorted by channel; ignore invalid channels and non-positive weights.
    // Every channel must clamp first, then use a continuous two-slope calibration curve.
    public static double[] Run(double[] input)
    {
        var groups = new SortedDictionary<int, List<(double x, double w)>>();
        for (int i = 0; i + 2 < input.Length; i += 3)
        {
            double raw = input[i];
            int key = (int)raw;
            double weight = input[i + 2];
""" + f"            if (raw != key || key < 0 || key >= {channels} || weight <= 0) continue;\n" + """            double value;
            switch (key)
            {
"""
    source += "".join(f"                case {i}: value = Calibrate{i:02d}(input[i + 1]); break;\n" for i in range(channels))
    source += """                default: continue;
            }
            if (!groups.ContainsKey(key)) groups[key] = new List<(double, double)>();
            groups[key].Add((value, weight));
        }
        var result = new List<double>();
        foreach (var group in groups)
        {
            result.Add(group.Key);
            result.AddRange(FinishGroup(group.Value));
        }
        return result.ToArray();
    }
    private static double[] FinishGroup(List<(double x, double w)> items)
    {
        double weight = 0, sum = 0, squares = 0;
        foreach (var item in items)
        {
            weight += item.w;
            sum += item.x * item.w;
            squares += item.x * item.x * item.w;
        }
        double mean = sum / weight;
"""
    if hard:
        if bad:
            source += "        double deviation = Math.Sqrt(Math.Max(0, squares / weight + mean * mean));\n"
        else:
            source += "        double variance = 0;\n        foreach (var item in items) variance += item.w * (item.x - mean) * (item.x - mean);\n        double deviation = Math.Sqrt(Math.Max(0, variance / weight));\n"
        source += "        return new double[] { mean, deviation, items.Count };\n"
    else:
        source += "        return new double[] { mean, items.Count };\n"
    source += "    }\n"
    for i in range(channels):
        left, right = (i%7+1)/10, (i%5+2)/10
        source += f"""    // Channel {i}: lower={-20-i%4}, upper={45+i%13}, pivot={(i%9+1)*3}.
    // Left gain={left:.1f}, right gain={right:.1f}, offset={i%3}; continuous at pivot.
    private static double Calibrate{i:02d}(double value)
    {{
        const double lower = {-20-i%4};
        const double upper = {45+i%13};
        const double pivot = {(i%9+1)*3};
        const double leftGain = {left:.1f};
        const double rightGain = {right:.1f};
        const double offset = {i%3};
        double clamped = value;
        if (clamped < lower)
        {{
            clamped = lower;
        }}
        if (clamped > upper)
        {{
            clamped = upper;
        }}
        if (clamped <= pivot)
        {{
            return clamped * leftGain + offset{' + 1' if bad and i == 0 else ''};
        }}
        double head = pivot * leftGain;
        double tail = (clamped - pivot) * (rightGain{' + 0.1' if bad and i == channels-1 else ''});
        return head + tail + offset;
    }}
"""
    return source + "}\n"


def stats(data):
    pairs = [(data[i], data[i+1]) for i in range(0, len(data)-1, 2) if data[i+1] > 0]
    if not pairs:
        return [0]*5
    weight = math.fsum(w for _, w in pairs)
    mean = math.fsum(x*w for x, w in pairs)/weight
    return [mean, math.fsum(w*(x-mean)**2 for x, w in pairs)/weight,
            min(x for x, _ in pairs), max(x for x, _ in pairs), len(pairs)]


STATS_CS = """using System;
using System.Collections.Generic;
public static class Script
{
    public static double[] Run(double[] input)
    {
        double weight=0, sum=0, min=double.PositiveInfinity, max=double.NegativeInfinity;
        int count=0;
        for(int i=0;i+1<input.Length;i+=2) if(input[i+1]>0)
        { weight+=input[i+1]; sum+=input[i]*input[i+1]; min=Math.Min(min,input[i]); max=Math.Max(max,input[i]); count++; }
        if(count==0) return new double[5];
        double mean=sum/weight, variance=0;
        for(int i=0;i+1<input.Length;i+=2) if(input[i+1]>0) variance+=input[i+1]*Math.Pow(input[i]-mean,2);
        return new double[]{mean,variance/weight,min,max,count};
    }
}
"""


def grouped(data):
    groups = {}
    for i in range(0, len(data)-2, 3):
        key, x, w = data[i:i+3]
        if key != int(key) or w <= 0:
            continue
        groups.setdefault(int(key), []).append((x, w))
    result = []
    for key, items in sorted(groups.items()):
        total = math.fsum(w for _, w in items)
        mean = math.fsum(x*w for x, w in items)/total
        variance = math.fsum(w*(x-mean)**2 for x, w in items)/total
        cumulative, median = 0, 0
        for x, w in sorted(items):
            cumulative += w
            if cumulative >= total/2:
                median = x
                break
        result += [key, mean, variance, median]
    return result


GROUPED_CS = """using System;
using System.Collections.Generic;
using System.Linq;
public static class Script
{
    public static double[] Run(double[] input)
    {
        var groups=new SortedDictionary<int,List<(double x,double w)>>();
        for(int i=0;i+2<input.Length;i+=3)
        {
            int k=(int)input[i]; if(k!=input[i] || input[i+2]<=0) continue;
            if(!groups.ContainsKey(k)) groups[k]=new List<(double,double)>();
            groups[k].Add((input[i+1],input[i+2]));
        }
        var result=new List<double>();
        foreach(var g in groups)
        {
            double total=g.Value.Sum(p=>p.w), mean=g.Value.Sum(p=>p.x*p.w)/total;
            double variance=g.Value.Sum(p=>p.w*Math.Pow(p.x-mean,2))/total;
            double cumulative=0, median=0;
            foreach(var p in g.Value.OrderBy(p=>p.x)) { cumulative+=p.w; if(cumulative>=total/2) {median=p.x;break;} }
            result.AddRange(new double[]{g.Key,mean,variance,median});
        }
        return result.ToArray();
    }
}
"""


def integrate(data):
    if len(data) < 6 or data[1] <= data[0]:
        return [0]*6
    knots = {}
    for i in range(2, len(data)-1, 2):
        knots.setdefault(data[i], []).append(data[i+1])
    points = sorted((x, math.fsum(ys)/len(ys)) for x, ys in knots.items())
    area, squares, positive, length = 0., 0., 0., 0.
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        a, b = max(x0, data[0]), min(x1, data[1])
        if b <= a:
            continue
        ya = y0 + (y1-y0)*(a-x0)/(x1-x0)
        yb = y0 + (y1-y0)*(b-x0)/(x1-x0)
        width = b-a
        area += width*(ya+yb)/2
        squares += width*(ya*ya+ya*yb+yb*yb)/3
        length += width
        if ya >= 0 and yb >= 0:
            positive += width*(ya+yb)/2
        elif ya > 0:
            positive += width*ya*ya/(2*(ya-yb))
        elif yb > 0:
            positive += width*yb*yb/(2*(yb-ya))
    return [area, squares, positive, length, area/length if length else 0,
            math.sqrt(max(0, squares/length)) if length else 0]


INTEGRATE_CS = """using System;
using System.Collections.Generic;
using System.Linq;
public static class Script
{
    public static double[] Run(double[] input)
    {
        if(input.Length<6 || input[1]<=input[0]) return new double[6];
        var knots=new SortedDictionary<double,List<double>>();
        for(int i=2;i+1<input.Length;i+=2) {if(!knots.ContainsKey(input[i]))knots[input[i]]=new List<double>();knots[input[i]].Add(input[i+1]);}
        var points=knots.Select(p=>(x:p.Key,y:p.Value.Average())).ToArray();
        double area=0,squares=0,positive=0,length=0;
        for(int i=0;i+1<points.Length;i++)
        {
            var p=points[i];var q=points[i+1];
            double a=Math.Max(p.x,input[0]),b=Math.Min(q.x,input[1]);if(b<=a)continue;
            double ya=p.y+(q.y-p.y)*(a-p.x)/(q.x-p.x),yb=p.y+(q.y-p.y)*(b-p.x)/(q.x-p.x),w=b-a;
            area+=w*(ya+yb)/2;squares+=w*(ya*ya+ya*yb+yb*yb)/3;length+=w;
            if(ya>=0 && yb>=0)positive+=w*(ya+yb)/2;
            else if(ya>0)positive+=w*ya*ya/(2*(ya-yb));
            else if(yb>0)positive+=w*yb*yb/(2*(yb-ya));
        }
        return new double[]{area,squares,positive,length,length>0?area/length:0,length>0?Math.Sqrt(Math.Max(0,squares/length)):0};
    }
}
"""


def make_cases():
    rng = random.Random(20261026)
    cases = []
    for level, channels, hard, cap in (("easy", 1, False, 100), ("medium", 22, False, 1000), ("hard", 72, True, 3000)):
        tests = [[], [0, 3, 1], [channels-1, 100, 2], [-1, 1, 2], [0, 1, 0], [0, -30, 1, 0, 100, 3],
                 [0.5, 10, 2], [0, 2, 1, 0, 2, 1]]
        for _ in range(10):
            tests.append([v for k in range(channels) for v in (k, rng.randint(-40, 100), rng.randint(-1, 8))])
        tests.append([v for _ in range(1500) for v in (rng.randrange(channels), rng.uniform(-40, 100), rng.uniform(.1, 4))])
        sample = [0, -2, 1, 0, 4, 2, channels-1, 60, 3]
        faulty = ("Calibrate00", f"Calibrate{channels-1:02d}")
        faulty = tuple(dict.fromkeys(faulty)) + (("FinishGroup",) if hard else ())
        spec = (f"输入是完整三元组[channel,value,weight]的扁平数组，channel必须是0..{channels-1}的整数，weight>0；否则忽略该三元组。"
                "每个通道先按各函数注释给出的lower/upper裁剪value，再按pivot左右增益作连续分段线性转换并加offset。"
                "以channel升序输出各组[channel,加权平均," + ("加权总体标准差," if hard else "") + "有效记录数]，扁平拼接；无记录输出空数组。"
                + ("总体方差为各记录与加权均值的平方偏差的加权平均。" if hard else "")
                + "输入不应被修改。修正与上述约定不符的现有实现。")
        cases.append(Case(f"{level}-read", level, "read", cap, "通道分段校准与分组统计", spec,
                          calibration_source(channels, hard, True), calibration_source(channels, hard, False),
                          tuple(tests), tuple(calibration(t, channels, hard) for t in tests), tuple(sample),
                          tuple(calibration(sample, channels, hard, True)), faulty))
    paired = [[], [1, 0], [4, 1], [2, 1, 6, 3], [-5, 2, 5, 2], [2, -1, 9, 1]]
    paired += [[1.5, .25, -2, 1.75], [1e6, .5, 999999.99, .5]]
    paired += [[v for _ in range(80) for v in (rng.uniform(-100, 100), rng.randint(-2, 10))] for _ in range(12)]
    triples = [[], [0, 4, 1], [0, 1, 1, 0, 3, 1], [1, 4, 0], [2, 8, 1, -2, 0, 1], [0.5, 7, 1]]
    triples += [[v for _ in range(120) for v in (rng.randint(-5, 6), rng.randint(-50, 50), rng.randint(-2, 8))] for _ in range(12)]
    curves = [[], [0, 0, 0, 1, 1, 3], [0, 2, 0, -1, 2, 1], [0, 2, 0, 1, 2, -1],
              [0, 4, 0, 1, 2, 3, 2, 5, 4, -1], [10, 20, 0, 1, 2, 3], [0, 2, 1, 4, 1, 6],
              [1, 3, 0, -4, 2, 2, 4, -2]]
    curves += [[-5, 12] + [v for _ in range(80) for v in (rng.randint(-10, 15), rng.uniform(-50, 50))] for _ in range(12)]
    tasks = [
        ("easy", 100, "加权统计", "输入是完整[value,weight]对的扁平数组，忽略weight<=0的记录。返回[加权均值,加权总体方差,最小值,最大值,有效记录数]；无有效记录返回五个0。", stats, paired, STATS_CS),
        ("medium", 1000, "分组加权统计与分位数", "输入是完整[key,value,weight]三元组的扁平数组，忽略非整数key或weight<=0的记录。按key升序，扁平返回每组[key,加权均值,加权总体方差,加权中位数]；加权中位数定义为按value升序累计权重首次达到总权重一半的value；无有效记录返回空数组。", grouped, triples, GROUPED_CS),
        ("hard", 3000, "分段曲线裁剪积分", "输入前两个数为区间L,R，后面是完整[x,y]坐标对，均为有限数。相同x的y取算术平均，再按x排序，相邻节点之间线性插值，仅在节点覆盖范围与[L,R]的交集积分，不外推。返回[有符号积分,平方函数积分,正部积分,覆盖长度,覆盖区间均值,覆盖区间均方根]。正部指max(y,0)，跨零需准确分割。R<=L、少于两个不同x或没有覆盖时返回六个0。", integrate, curves, INTEGRATE_CS),
    ]
    for level, cap, title, spec, oracle, tests, reference in tasks:
        cases.append(Case(f"{level}-write", level, "write", cap, title, spec + " 输入不应被修改。", SKELETON,
                          reference, tuple(tests), tuple(oracle(t) for t in tests)))
    return tuple(sorted(cases, key=lambda c: (("easy", "medium", "hard").index(c.level), c.kind)))


def close_numbers(actual, expected):
    return (isinstance(actual, list) and len(actual) == len(expected)
            and all(type(a) in (int, float) and math.isfinite(a)
                    and math.isclose(a, b, rel_tol=1e-7, abs_tol=1e-8) for a, b in zip(actual, expected)))


def analysis_result(text, case):
    decoder = json.JSONDecoder()
    for i, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[i:])
            return {"faults_correct": set(value.get("faultyFunctions", [])) == set(case.faulty),
                    "sample_correct": close_numbers(value.get("sampleResult"), case.sample_result)}
        except (ValueError, TypeError, AttributeError):
            continue
    return {"faults_correct": False, "sample_correct": False, "error": "没有可判读的分析JSON"}
