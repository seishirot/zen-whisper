// swift-tools-version: 5.9

import PackageDescription

let package = Package(
    name: "ZenWhisper",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "zen-whisper", targets: ["ZenWhisper"])
    ],
    targets: [
        .executableTarget(
            name: "ZenWhisper",
            path: "ZenWhisper",
            exclude: ["Info.plist"],
            resources: [
                .copy("Resources")
            ]
        ),
        .testTarget(
            name: "ZenWhisperTests",
            dependencies: ["ZenWhisper"],
            path: "ZenWhisperTests"
        )
    ]
)
