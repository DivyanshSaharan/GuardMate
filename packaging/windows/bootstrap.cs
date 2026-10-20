// Unsigned bootstrap: the appended account frame is a credential, not an Authenticode signature.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Net;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal static class Bootstrap {
    const string Origin = "https://guardmate.onrender.com";
    const string Magic = "GMATE-ENVELOPE1!"; // 16 bytes; 52-byte footer including hash and length.
    static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 65536 };
    static readonly string Root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "GuardMate");
    static readonly string Self = Assembly.GetExecutingAssembly().Location;
    static volatile bool Cancelled;
    static Form Window;
    static Label Status;
    static ProgressBar Progress;
    static string Stage = "prepare";

    [STAThread] static int Main(string[] args) {
        ServicePointManager.SecurityProtocol = SecurityProtocolType.Tls12;
        try {
            if (args.Length == 1 && args[0] == "--uninstall") return Uninstall();
            var envelope = ReadEnvelope();
            if (args.Length == 1 && args[0] == "--verify-envelope") return 0;
            if (args.Length == 2 && args[0] == "--probe-envelope") {
                // Offline interoperability proof only. No account exchange, download or GUI.
                var probe = NewSession(envelope, "probe-access", "probe-account", 0);
                File.WriteAllBytes(args[1], ProtectedData.Protect(Encoding.UTF8.GetBytes(Json.Serialize(probe)), null, DataProtectionScope.CurrentUser));
                return 0;
            }
            if (args.Length != 0) return 2;
            ShowInstaller(envelope);
            return 0;
        } catch {
            if (args.Length == 0) MessageBox.Show("This GuardMate installer is incomplete or invalid. Download a fresh installer from your account.", "GuardMate Setup", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 2;
        }
    }

    static Dictionary<string, object> ReadEnvelope() {
        using (var file = File.OpenRead(Self)) using (var reader = new BinaryReader(file)) {
            if (file.Length < 54) throw new InvalidDataException();
            file.Seek(-52, SeekOrigin.End);
            byte[] expected = reader.ReadBytes(32);
            uint length = reader.ReadUInt32();
            if (Encoding.ASCII.GetString(reader.ReadBytes(16)) != Magic || length < 1 || length > 16384 || file.Length < length + 52) throw new InvalidDataException();
            file.Seek(-52 - length, SeekOrigin.End);
            byte[] body = reader.ReadBytes((int)length);
            using (var hash = SHA256.Create()) if (!hash.ComputeHash(body).SequenceEqual(expected)) throw new InvalidDataException();
            var envelope = Json.Deserialize<Dictionary<string, object>>(new UTF8Encoding(false, true).GetString(body));
            if (envelope == null || envelope.Count != 3 || !envelope.ContainsKey("version") || !(envelope["version"] is int) || (int)envelope["version"] != 1) throw new InvalidDataException();
            if (Text(envelope, "server_url") != Origin || Text(envelope, "bootstrap_token").Length > 4096) throw new InvalidDataException();
            return envelope;
        }
    }

    static string Text(Dictionary<string, object> obj, string key) {
        object value;
        if (!obj.TryGetValue(key, out value) || !(value is string) || String.IsNullOrWhiteSpace((string)value)) throw new InvalidDataException();
        return (string)value;
    }

    static void ShowInstaller(Dictionary<string, object> envelope) {
        Application.EnableVisualStyles();
        Window = new Form { Text = "GuardMate Setup", ClientSize = new Size(530, 280), StartPosition = FormStartPosition.CenterScreen, FormBorderStyle = FormBorderStyle.FixedDialog, MaximizeBox = false };
        try { Window.Icon = Icon.ExtractAssociatedIcon(Self); }
        catch (ArgumentException) { }
        catch (IOException) { }
        catch (System.ComponentModel.Win32Exception) { }
        var title = new Label { Text = "Install GuardMate for this Windows user", Location = new Point(22, 20), Size = new Size(490, 26), Font = new Font("Segoe UI", 12, FontStyle.Bold) };
        var description = new Label { Text = "Setup downloads the Windows app and verified English speech models.\nYour copied installer stays linked to the same account and usage allowance.\nPhone Link pairing and microphone permissions are completed in the app.", Location = new Point(22, 56), Size = new Size(490, 65) };
        Status = new Label { Text = "Ready. Close GuardMate before installing an update.", Location = new Point(22, 128), Size = new Size(490, 60) };
        Progress = new ProgressBar { Location = new Point(22, 195), Size = new Size(490, 18) };
        var button = new Button { Text = "Install and open", Location = new Point(362, 234), Size = new Size(150, 29) };
        EventHandler installHandler = null;
        installHandler = async delegate {
            button.Enabled = false;
            try {
                await Task.Run(() => Install(envelope));
                Update("Installed. GuardMate is opening.", 100);
            } catch (Exception error) {
                ReportFailure(error);
            } finally {
                button.Click -= installHandler;
                button.Text = "Close"; button.Enabled = true;
                button.Click += delegate { Window.Close(); };
            }
        };
        button.Click += installHandler;
        Window.FormClosing += delegate { Cancelled = true; };
        Window.Controls.AddRange(new Control[] { title, description, Status, Progress, button });
        Application.Run(Window);
    }

    static void Update(string text, int percent) {
        if (Cancelled) throw new OperationCanceledException();
        if (Window == null) return;
        Window.Invoke((Action)delegate { Status.Text = text; Progress.Value = Math.Max(0, Math.Min(100, percent)); });
    }

    static void ReportFailure(Exception error) {
        string code = Stage;
        var network = error as WebException;
        var response = network == null ? null : network.Response as HttpWebResponse;
        if (response != null) code += "-http-" + (int)response.StatusCode;
        // Exception messages can contain signed URLs or credentials. Record only safe classifications.
        try {
            Directory.CreateDirectory(Root);
            File.AppendAllText(Path.Combine(Root, "setup.log"), DateTime.UtcNow.ToString("o") + " " + code + " " + error.GetType().Name + Environment.NewLine);
        } catch { }
        Update("Setup stopped (" + code + "). Details: %LOCALAPPDATA%\\GuardMate\\setup.log. Close setup and try again. No calls were started.", 0);
    }

    static bool RuntimeRunning() {
        try { using (Mutex.OpenExisting("Local\\GuardMateDesktop")) return true; }
        catch (WaitHandleCannotBeOpenedException) { return false; }
    }

    static Dictionary<string, object> Api(string path, string access, Dictionary<string, object> body = null) {
        var request = (HttpWebRequest)WebRequest.Create(Origin + path);
        request.Method = body == null ? "GET" : "POST";
        request.AllowAutoRedirect = false;
        request.Timeout = 45000; request.ReadWriteTimeout = 45000;
        if (access != null) request.Headers[HttpRequestHeader.Authorization] = "Bearer " + access;
        if (body != null) {
            byte[] data = Encoding.UTF8.GetBytes(Json.Serialize(body));
            request.ContentType = "application/json"; request.ContentLength = data.Length;
            using (var stream = request.GetRequestStream()) stream.Write(data, 0, data.Length);
        }
        using (var response = (HttpWebResponse)request.GetResponse()) using (var stream = response.GetResponseStream()) using (var output = new MemoryStream()) {
            if ((int)response.StatusCode != 200) throw new InvalidDataException();
            byte[] buffer = new byte[8192]; int read;
            while ((read = stream.Read(buffer, 0, buffer.Length)) > 0) { if (output.Length + read > 65536) throw new InvalidDataException(); output.Write(buffer, 0, read); }
            return Json.Deserialize<Dictionary<string, object>>(Encoding.UTF8.GetString(output.ToArray()));
        }
    }

    static Dictionary<string, object> NewSession(Dictionary<string, object> envelope, string access, string account, double expires) {
        return new Dictionary<string, object> { { "version", 1 }, { "server_url", Origin }, { "bootstrap_token", Text(envelope, "bootstrap_token") }, { "access_token", access }, { "account_id", account }, { "expires_at", expires } };
    }

    static void Download(string url, string target, string expectedHash, long expectedSize, string message) {
        if (expectedSize < 1 || expectedSize > 800000000 || expectedHash.Length != 64 || expectedHash.Any(c => !Uri.IsHexDigit(c))) throw new InvalidDataException();
        if (File.Exists(target)) {
            try { Verify(target, expectedHash, expectedSize); return; }
            catch (InvalidDataException) { File.Delete(target); }
        }
        string temporary = target + "." + Guid.NewGuid().ToString("N") + ".partial";
        try {
            Uri current = new Uri(url); HttpWebResponse response = null;
            for (int redirects = 0; redirects <= 5; redirects++) {
                if (current.Scheme != "https" || !String.IsNullOrEmpty(current.UserInfo)) throw new InvalidDataException();
                var request = (HttpWebRequest)WebRequest.Create(current);
                request.AllowAutoRedirect = false; request.Timeout = 45000; request.ReadWriteTimeout = 45000;
                response = (HttpWebResponse)request.GetResponse();
                if ((int)response.StatusCode == 200) break;
                if ((int)response.StatusCode < 300 || (int)response.StatusCode > 399 || redirects == 5) { response.Dispose(); throw new InvalidDataException(); }
                current = new Uri(current, response.Headers[HttpResponseHeader.Location]); response.Dispose(); response = null;
            }
            if (response == null) throw new InvalidDataException();
            using (response) using (var source = response.GetResponseStream()) using (var output = new FileStream(temporary, FileMode.CreateNew)) {
                var watch = Stopwatch.StartNew(); byte[] buffer = new byte[65536]; long total = 0; int read;
                while ((read = source.Read(buffer, 0, buffer.Length)) > 0) {
                    total += read;
                    if (Cancelled || total > expectedSize || watch.Elapsed.TotalSeconds > 600) throw new InvalidDataException();
                    output.Write(buffer, 0, read); Update(message, (int)(total * 100 / expectedSize));
                }
            }
            Verify(temporary, expectedHash, expectedSize);
            File.Move(temporary, target);
        } finally { if (File.Exists(temporary)) File.Delete(temporary); }
    }

    static void Verify(string path, string digest, long size) {
        if (new FileInfo(path).Length != size) throw new InvalidDataException();
        using (var stream = File.OpenRead(path)) using (var hash = SHA256.Create()) {
            if (BitConverter.ToString(hash.ComputeHash(stream)).Replace("-", "").ToLowerInvariant() != digest.ToLowerInvariant()) throw new InvalidDataException();
        }
    }

    static string ChildPath(string root, string relative) {
        if (String.IsNullOrWhiteSpace(relative) || relative.Contains("\\") || relative.Contains(":") || Path.IsPathRooted(relative)) throw new InvalidDataException();
        foreach (string part in relative.Split('/')) if (part == ".." || part == "." || part.EndsWith(".") || part.EndsWith(" ")) throw new InvalidDataException();
        string result = Path.GetFullPath(Path.Combine(root, relative));
        if (!result.StartsWith(Path.GetFullPath(root) + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException();
        return result;
    }

    static void Extract(string zip, string destination) {
        long total = 0; int count = 0;
        Directory.CreateDirectory(destination);
        using (var archive = ZipFile.OpenRead(zip)) foreach (var entry in archive.Entries) {
            if (++count > 20000 || entry.Length > 500000000 || (total += entry.Length) > 1500000000 || ((entry.ExternalAttributes >> 16) & 0xF000) == 0xA000) throw new InvalidDataException();
            // Windows Compress-Archive emits backslashes. Validate the normalized path before writing.
            string relative = entry.FullName.Replace('\\', '/');
            string target = ChildPath(destination, relative);
            if (relative.EndsWith("/")) { Directory.CreateDirectory(target); continue; }
            Directory.CreateDirectory(Path.GetDirectoryName(target));
            using (var input = entry.Open()) using (var output = new FileStream(target, FileMode.CreateNew)) {
                byte[] buffer = new byte[65536]; long written = 0; int read;
                while ((read = input.Read(buffer, 0, buffer.Length)) > 0) { written += read; if (Cancelled || written > entry.Length) throw new InvalidDataException(); output.Write(buffer, 0, read); }
                if (written != entry.Length) throw new InvalidDataException();
            }
        }
    }

    static List<Dictionary<string, object>> ReadSpeechAssets(string path) {
        var manifest = Json.Deserialize<Dictionary<string, object>>(File.ReadAllText(path));
        var assets = new List<Dictionary<string, object>>();
        // JavaScriptSerializer represents nested JSON arrays as ArrayList on .NET Framework.
        foreach (object item in (System.Collections.IEnumerable)manifest["assets"]) {
            var asset = item as Dictionary<string, object>;
            if (asset == null || assets.Count >= 8) throw new InvalidDataException();
            assets.Add(asset);
        }
        if (assets.Count == 0) throw new InvalidDataException();
        return assets;
    }

    static void Install(Dictionary<string, object> envelope) {
        bool owner;
        using (var mutex = new Mutex(true, "Local\\GuardMateInstaller", out owner)) {
            Stage = "application-running";
            if (!owner || RuntimeRunning()) throw new InvalidOperationException();
            Stage = "prepare";
            Directory.CreateDirectory(Root);
            Stage = "activate";
            Update("Activating this account...", 0);
            var activation = Api("/api/cloud/activate", null, new Dictionary<string, object> { { "credential", Text(envelope, "bootstrap_token") } });
            string access = Text(activation, "access_token"), account = Text(activation, "account_id");
            double expires = DateTime.UtcNow.Subtract(new DateTime(1970, 1, 1)).TotalSeconds + Convert.ToInt32(activation["expires_in"]);
            Stage = "release";
            var release = Api("/api/cloud/release", access);
            string version = Text(release, "version");
            if (version.Length > 80 || version.Any(c => !(Char.IsLetterOrDigit(c) || c == '.' || c == '-'))) throw new InvalidDataException();
            string releases = Path.Combine(Root, "releases"), target = ChildPath(releases, version);
            Directory.CreateDirectory(releases);
            if (!File.Exists(Path.Combine(target, "GuardMate.exe"))) {
                string cache = Path.Combine(Root, "downloads"); Directory.CreateDirectory(cache);
                string archive = Path.Combine(cache, "runtime-" + version + ".zip");
                Stage = "runtime-download";
                Download(Text(release, "url"), archive, Text(release, "sha256"), Convert.ToInt64(release["size"]), "Downloading the Windows application...");
                string staging = ChildPath(releases, "staging-" + Guid.NewGuid().ToString("N"));
                try {
                    Stage = "runtime-extract";
                    Update("Verifying and extracting the application...", 0); Extract(archive, staging);
                    if (!File.Exists(Path.Combine(staging, "GuardMate.exe"))) throw new InvalidDataException();
                    Directory.Move(staging, target);
                } finally { if (Directory.Exists(staging)) Directory.Delete(staging, true); }
            }
            Stage = "speech-manifest";
            foreach (var asset in ReadSpeechAssets(Path.Combine(target, "_internal", "speech-assets.json"))) {
                string relative = Text(asset, "path");
                if (!relative.StartsWith("models/speech/", StringComparison.Ordinal)) throw new InvalidDataException();
                string model = ChildPath(Root, relative); Directory.CreateDirectory(Path.GetDirectoryName(model));
                Stage = "speech-download";
                Download(Text(asset, "url"), model, Text(asset, "sha256"), Convert.ToInt64(asset["size"]), "Preparing verified English speech models...");
            }
            Stage = "session-save";
            if (RuntimeRunning() || Cancelled) throw new InvalidOperationException();
            byte[] protectedSession = ProtectedData.Protect(Encoding.UTF8.GetBytes(Json.Serialize(NewSession(envelope, access, account, expires))), null, DataProtectionScope.CurrentUser);
            string temporary = Path.Combine(Root, "session.new"); File.WriteAllBytes(temporary, protectedSession);
            string sessionFile = Path.Combine(Root, "session.dat");
            if (File.Exists(sessionFile)) File.Replace(temporary, sessionFile, null); else File.Move(temporary, sessionFile);
            Stage = "shortcut";
            dynamic shell = Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell"));
            dynamic shortcut = shell.CreateShortcut(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), "GuardMate.lnk"));
            shortcut.TargetPath = Path.Combine(target, "GuardMate.exe"); shortcut.WorkingDirectory = target;
            shortcut.IconLocation = Path.Combine(target, "GuardMate.exe") + ",0"; shortcut.Save();
            Stage = "uninstaller";
            WriteUninstaller();
            Stage = "launch";
            Process.Start(new ProcessStartInfo(Path.Combine(target, "GuardMate.exe")) { WorkingDirectory = target, UseShellExecute = true });
        }
    }

    static void WriteUninstaller() {
        byte[] original = File.ReadAllBytes(Self);
        uint envelopeSize = BitConverter.ToUInt32(original, original.Length - 20);
        string executable = Path.Combine(Root, "Uninstall.exe");
        using (var output = File.Create(executable)) output.Write(original, 0, original.Length - 52 - (int)envelopeSize);
        using (var key = Microsoft.Win32.Registry.CurrentUser.CreateSubKey(@"Software\Microsoft\Windows\CurrentVersion\Uninstall\GuardMate")) {
            key.SetValue("DisplayName", "GuardMate"); key.SetValue("Publisher", "GuardMate");
            key.SetValue("UninstallString", "\"" + executable + "\" --uninstall"); key.SetValue("InstallLocation", Root);
            key.SetValue("DisplayIcon", executable + ",0");
            key.SetValue("NoModify", 1); key.SetValue("NoRepair", 1);
        }
    }

    static int Uninstall() {
        if (RuntimeRunning()) { MessageBox.Show("Close GuardMate before uninstalling.", "GuardMate"); return 2; }
        if (MessageBox.Show("Remove the GuardMate application? Saved local preferences and speech models will remain.", "GuardMate", MessageBoxButtons.YesNo) != DialogResult.Yes) return 0;
        string releases = Path.GetFullPath(Path.Combine(Root, "releases"));
        if (!releases.StartsWith(Path.GetFullPath(Root) + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)) return 2;
        if (Directory.Exists(releases)) Directory.Delete(releases, true);
        string shortcut = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), "GuardMate.lnk");
        if (File.Exists(shortcut)) File.Delete(shortcut);
        string session = Path.Combine(Root, "session.dat"); if (File.Exists(session)) File.Delete(session);
        Microsoft.Win32.Registry.CurrentUser.DeleteSubKeyTree(@"Software\Microsoft\Windows\CurrentVersion\Uninstall\GuardMate", false);
        return 0;
    }
}
