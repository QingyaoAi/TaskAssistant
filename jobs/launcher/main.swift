// TaskAssistant：个人助理的作业启动器。
//
// launchd 通过它运行 jobs/run.py，这样 macOS 的隐私授权（读 iCloud 云盘里的收件箱）只需要给
// 「TaskAssistant」这一个 app 一次；系统自带的 python3 是平台二进制，系统不会为它弹授权窗口。
// 用法：TaskAssistant <inbox|mail|morning|maintain> [--force]
import Foundation

let project = Bundle.main.object(forInfoDictionaryKey: "AssistantProject") as? String
    ?? "\(NSHomeDirectory())/Agent Workspace/个人助理"

let child = Process()
child.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
child.arguments = ["\(project)/jobs/run.py"] + CommandLine.arguments.dropFirst()
child.currentDirectoryURL = URL(fileURLWithPath: project)

// launchd 停止作业时把信号转给子进程。
for sig in [SIGTERM, SIGINT] {
    signal(sig, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    source.setEventHandler { child.terminate() }
    source.resume()
}

do {
    try child.run()
} catch {
    FileHandle.standardError.write("无法启动 run.py：\(error)\n".data(using: .utf8)!)
    exit(1)
}
DispatchQueue.global().async {
    child.waitUntilExit()
    exit(child.terminationStatus)
}
dispatchMain()
