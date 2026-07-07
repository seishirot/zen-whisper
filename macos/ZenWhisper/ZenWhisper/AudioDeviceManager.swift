import AudioToolbox
import AVFoundation
import CoreAudio
import Foundation

struct AudioInputDevice: Equatable {
    let id: AudioDeviceID
    let uid: String
    let name: String
}

enum AudioDeviceError: Error, CustomStringConvertible {
    case selectedInputDeviceUnavailable(String)
    case couldNotApplyInputDevice(OSStatus)

    var description: String {
        switch self {
        case .selectedInputDeviceUnavailable(let uid):
            return "Selected microphone is unavailable: \(uid)"
        case .couldNotApplyInputDevice(let status):
            return "Could not select microphone: OSStatus \(status)"
        }
    }
}

enum AudioDeviceManager {
    static func inputDevices() -> [AudioInputDevice] {
        allAudioDeviceIDs()
            .compactMap { deviceID in
                guard inputChannelCount(for: deviceID) > 0,
                      let uid = stringProperty(
                        deviceID,
                        selector: kAudioDevicePropertyDeviceUID,
                        scope: kAudioObjectPropertyScopeGlobal
                      ) else {
                    return nil
                }
                let name = stringProperty(
                    deviceID,
                    selector: kAudioObjectPropertyName,
                    scope: kAudioObjectPropertyScopeGlobal
                ) ?? "Microphone \(deviceID)"
                return AudioInputDevice(id: deviceID, uid: uid, name: name)
            }
            .sorted { lhs, rhs in
                lhs.name.localizedCaseInsensitiveCompare(rhs.name) == .orderedAscending
            }
    }

    static func validInputDeviceUID(_ uid: String?) -> String? {
        guard let uid, !uid.isEmpty else {
            return nil
        }
        return deviceID(forUID: uid) == nil ? nil : uid
    }

    static func deviceID(forUID uid: String) -> AudioDeviceID? {
        inputDevices().first { $0.uid == uid }?.id
    }

    static func defaultInputDevice() -> AudioInputDevice? {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultInputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var deviceID = AudioDeviceID(0)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        let status = AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject),
            &address,
            0,
            nil,
            &size,
            &deviceID
        )
        guard status == noErr, deviceID != 0 else {
            return nil
        }
        return inputDevices().first { $0.id == deviceID }
    }

    static func applyInputDevice(uid: String?, to inputNode: AVAudioInputNode) throws {
        let targetDeviceID: AudioDeviceID
        if let uid, !uid.isEmpty {
            guard let deviceID = deviceID(forUID: uid) else {
                throw AudioDeviceError.selectedInputDeviceUnavailable(uid)
            }
            targetDeviceID = deviceID
        } else {
            guard let defaultDevice = defaultInputDevice() else {
                return
            }
            targetDeviceID = defaultDevice.id
        }
        var deviceID = targetDeviceID
        guard let audioUnit = inputNode.audioUnit else {
            throw AudioDeviceError.couldNotApplyInputDevice(-1)
        }
        let status = AudioUnitSetProperty(
            audioUnit,
            kAudioOutputUnitProperty_CurrentDevice,
            kAudioUnitScope_Global,
            0,
            &deviceID,
            UInt32(MemoryLayout<AudioDeviceID>.size)
        )
        guard status == noErr else {
            throw AudioDeviceError.couldNotApplyInputDevice(status)
        }
    }

    private static func allAudioDeviceIDs() -> [AudioDeviceID] {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDevices,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var dataSize: UInt32 = 0
        let sizeStatus = AudioObjectGetPropertyDataSize(
            AudioObjectID(kAudioObjectSystemObject),
            &address,
            0,
            nil,
            &dataSize
        )
        guard sizeStatus == noErr, dataSize > 0 else {
            return []
        }
        let count = Int(dataSize) / MemoryLayout<AudioDeviceID>.size
        var devices = [AudioDeviceID](repeating: 0, count: count)
        let dataStatus = AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject),
            &address,
            0,
            nil,
            &dataSize,
            &devices
        )
        guard dataStatus == noErr else {
            return []
        }
        return devices
    }

    private static func inputChannelCount(for deviceID: AudioDeviceID) -> Int {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyStreamConfiguration,
            mScope: kAudioDevicePropertyScopeInput,
            mElement: kAudioObjectPropertyElementMain
        )
        var dataSize: UInt32 = 0
        let sizeStatus = AudioObjectGetPropertyDataSize(deviceID, &address, 0, nil, &dataSize)
        guard sizeStatus == noErr, dataSize > 0 else {
            return 0
        }
        let rawPointer = UnsafeMutableRawPointer.allocate(
            byteCount: Int(dataSize),
            alignment: MemoryLayout<AudioBufferList>.alignment
        )
        defer { rawPointer.deallocate() }
        let bufferList = rawPointer.bindMemory(to: AudioBufferList.self, capacity: 1)
        let dataStatus = AudioObjectGetPropertyData(
            deviceID,
            &address,
            0,
            nil,
            &dataSize,
            bufferList
        )
        guard dataStatus == noErr else {
            return 0
        }
        return UnsafeMutableAudioBufferListPointer(bufferList)
            .reduce(0) { total, buffer in total + Int(buffer.mNumberChannels) }
    }

    private static func stringProperty(
        _ deviceID: AudioDeviceID,
        selector: AudioObjectPropertySelector,
        scope: AudioObjectPropertyScope
    ) -> String? {
        var address = AudioObjectPropertyAddress(
            mSelector: selector,
            mScope: scope,
            mElement: kAudioObjectPropertyElementMain
        )
        let value = UnsafeMutablePointer<CFString?>.allocate(capacity: 1)
        value.initialize(to: nil)
        defer {
            value.deinitialize(count: 1)
            value.deallocate()
        }
        var size = UInt32(MemoryLayout<CFString?>.size)
        let status = AudioObjectGetPropertyData(deviceID, &address, 0, nil, &size, value)
        guard status == noErr else {
            return nil
        }
        return value.pointee as String?
    }
}
