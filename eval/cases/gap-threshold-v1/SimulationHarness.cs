// Deliberately synthetic SDK shim. This is not an IMV implementation.
using System;
using System.Collections.Generic;

namespace IMV.CoreBase
{
    public class Script_Function
    {
        public static double InputGap, Low, High;
        public static bool ThrowOnRead;
        public static Dictionary<string, object> Writes = new Dictionary<string, object>();
        public Script_Function(Guid processkey) { }
        public object GetValue(int id, string name)
        {
            if (ThrowOnRead) throw new InvalidOperationException("simulated read failure");
            if (id != 67 || name != "Distance") throw new ArgumentException("unknown output binding");
            return InputGap;
        }
        public object GetGlobalVarValue(string name)
        {
            if (ThrowOnRead) throw new InvalidOperationException("simulated read failure");
            if (name == "gap下限") return Low;
            if (name == "gap上限") return High;
            throw new ArgumentException("unknown global binding");
        }
        public void SetVarValue(int id, string name, object value)
        {
            if (id != 2 || (name != "gap" && name != "status" && name != "message"))
                throw new ArgumentException("unknown write binding");
            Writes[name] = value;
        }
    }
}

public static class Simulation
{
    public static int Failures;
    public static void Check(string name, double gap, double low, double high, bool throwOnRead,
                             int status, string message, double writtenGap)
    {
        IMV.CoreBase.Script_Function.InputGap = gap;
        IMV.CoreBase.Script_Function.Low = low;
        IMV.CoreBase.Script_Function.High = high;
        IMV.CoreBase.Script_Function.ThrowOnRead = throwOnRead;
        IMV.CoreBase.Script_Function.Writes.Clear();
        try
        {
            NamespaceCsharpScriptEngine.CsharpScriptClass.Execute(Guid.Empty);
            var result = IMV.CoreBase.Script_Function.Writes;
            bool pass = result.Count == 3 && result.ContainsKey("gap") && result.ContainsKey("status")
                     && result.ContainsKey("message") && result["gap"] is double && result["status"] is int
                     && (double)result["gap"] == writtenGap && (int)result["status"] == status
                     && (string)result["message"] == message;
            Console.WriteLine(name + ": " + (pass ? "PASS" : "FAIL"));
            if (!pass) Failures++;
        }
        catch (Exception error)
        {
            Console.WriteLine(name + ": FAIL " + error.GetType().Name);
            Failures++;
        }
    }
}
