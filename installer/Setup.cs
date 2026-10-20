// Self-extracting setup for KOMPAS MCP: unpacks the embedded install.ps1, payload.zip and VERSION
// to a temporary folder, runs install.ps1 in this console window and cleans up afterwards.
// Built by build_installer.ps1 with the C# compiler that ships with Windows (.NET Framework 4.x).
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;

static class Setup
{
    static int Main()
    {
        Console.Title = "KOMPAS MCP Setup";
        string dir = Path.Combine(Path.GetTempPath(), "KompasMCP-setup-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        try
        {
            Assembly asm = Assembly.GetExecutingAssembly();
            foreach (string name in new[] { "install.ps1", "payload.zip", "VERSION" })
            {
                using (Stream src = asm.GetManifestResourceStream(name))
                using (FileStream dst = File.Create(Path.Combine(dir, name)))
                    src.CopyTo(dst);
            }
            var psi = new ProcessStartInfo("powershell.exe",
                "-NoProfile -ExecutionPolicy Bypass -File \"" + Path.Combine(dir, "install.ps1") + "\"")
            {
                UseShellExecute = false,
                WorkingDirectory = dir,
            };
            using (Process p = Process.Start(psi))
            {
                p.WaitForExit();
                return p.ExitCode;
            }
        }
        catch (Exception e)
        {
            Console.Error.WriteLine("Setup failed: " + e.Message);
            Console.WriteLine("Press Enter to close.");
            Console.ReadLine();
            return 1;
        }
        finally
        {
            try { Directory.Delete(dir, true); } catch { }
        }
    }
}
