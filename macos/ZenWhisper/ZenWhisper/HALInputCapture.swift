import AudioToolbox
import CoreAudio
import Foundation

/// Captures mono Float32 samples from one specific input device through an
/// AUHAL unit.
///
/// The device is bound to the unit before `AudioUnitInitialize`, so no other
/// device is ever opened. `AVAudioEngine.inputNode` instead opens the system
/// default input as soon as it is created, which switches a default Bluetooth
/// headset into its hands-free (HFP) profile for the whole recording even
/// when a different microphone is selected.
final class HALInputCapture {
    let sampleRate: Double

    private let unit: AudioUnit
    private let channelCount: Int
    private let capacityFrames: Int
    private let renderBuffer: UnsafeMutablePointer<Float>
    private let onSamples: ([Float]) -> Void
    private var isInitialized = false
    private var isRunning = false
    private var isDisposed = false

    init(deviceID: AudioDeviceID, onSamples: @escaping ([Float]) -> Void) throws {
        self.onSamples = onSamples

        var description = AudioComponentDescription(
            componentType: kAudioUnitType_Output,
            componentSubType: kAudioUnitSubType_HALOutput,
            componentManufacturer: kAudioUnitManufacturer_Apple,
            componentFlags: 0,
            componentFlagsMask: 0
        )
        guard let component = AudioComponentFindNext(nil, &description) else {
            throw AudioRecorderError.microphoneUnavailable
        }
        var instance: AudioUnit?
        try Self.check(AudioComponentInstanceNew(component, &instance), "create")
        guard let instance else {
            throw AudioRecorderError.microphoneUnavailable
        }
        unit = instance

        do {
            var enable: UInt32 = 1
            var disable: UInt32 = 0
            try Self.check(
                AudioUnitSetProperty(
                    unit, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Input,
                    Self.inputElement, &enable, UInt32(MemoryLayout<UInt32>.size)
                ),
                "enable input"
            )
            try Self.check(
                AudioUnitSetProperty(
                    unit, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Output,
                    Self.outputElement, &disable, UInt32(MemoryLayout<UInt32>.size)
                ),
                "disable output"
            )

            var device = deviceID
            try Self.check(
                AudioUnitSetProperty(
                    unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global,
                    0, &device, UInt32(MemoryLayout<AudioDeviceID>.size)
                ),
                "select device"
            )

            var deviceFormat = AudioStreamBasicDescription()
            var formatSize = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
            try Self.check(
                AudioUnitGetProperty(
                    unit, kAudioUnitProperty_StreamFormat, kAudioUnitScope_Input,
                    Self.inputElement, &deviceFormat, &formatSize
                ),
                "read device format"
            )
            guard deviceFormat.mChannelsPerFrame > 0, deviceFormat.mSampleRate > 0 else {
                throw AudioRecorderError.microphoneUnavailable
            }
            sampleRate = deviceFormat.mSampleRate
            channelCount = Int(deviceFormat.mChannelsPerFrame)

            let bytesPerFrame = UInt32(MemoryLayout<Float>.size * channelCount)
            var clientFormat = AudioStreamBasicDescription(
                mSampleRate: sampleRate,
                mFormatID: kAudioFormatLinearPCM,
                mFormatFlags: kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked,
                mBytesPerPacket: bytesPerFrame,
                mFramesPerPacket: 1,
                mBytesPerFrame: bytesPerFrame,
                mChannelsPerFrame: UInt32(channelCount),
                mBitsPerChannel: 32,
                mReserved: 0
            )
            do {
                try Self.check(
                    AudioUnitSetProperty(
                        unit, kAudioUnitProperty_StreamFormat, kAudioUnitScope_Output,
                        Self.inputElement, &clientFormat,
                        UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
                    ),
                    "set client format"
                )
            } catch {
                throw AudioRecorderError.outputFormatUnavailable
            }

            var maxFrames: UInt32 = 0
            var maxFramesSize = UInt32(MemoryLayout<UInt32>.size)
            let maxFramesStatus = AudioUnitGetProperty(
                unit, kAudioUnitProperty_MaximumFramesPerSlice, kAudioUnitScope_Global,
                0, &maxFrames, &maxFramesSize
            )
            capacityFrames = Int(maxFramesStatus == noErr && maxFrames > 0 ? maxFrames : 4096)
            renderBuffer = .allocate(capacity: capacityFrames * channelCount)
        } catch {
            AudioComponentInstanceDispose(unit)
            throw error
        }

        do {
            var callback = AURenderCallbackStruct(
                inputProc: halInputCallback,
                inputProcRefCon: Unmanaged.passUnretained(self).toOpaque()
            )
            try Self.check(
                AudioUnitSetProperty(
                    unit, kAudioOutputUnitProperty_SetInputCallback, kAudioUnitScope_Global,
                    0, &callback, UInt32(MemoryLayout<AURenderCallbackStruct>.size)
                ),
                "set input callback"
            )
            try Self.check(AudioUnitInitialize(unit), "initialize")
            isInitialized = true
        } catch {
            dispose()
            throw error
        }
    }

    deinit {
        dispose()
    }

    func start() throws {
        try Self.check(AudioOutputUnitStart(unit), "start")
        isRunning = true
    }

    /// Stops capture and releases the device. Safe to call more than once.
    func dispose() {
        guard !isDisposed else {
            return
        }
        isDisposed = true
        if isRunning {
            AudioOutputUnitStop(unit)
            isRunning = false
        }
        if isInitialized {
            AudioUnitUninitialize(unit)
            isInitialized = false
        }
        AudioComponentInstanceDispose(unit)
        renderBuffer.deallocate()
    }

    fileprivate func render(
        flags: UnsafeMutablePointer<AudioUnitRenderActionFlags>,
        timeStamp: UnsafePointer<AudioTimeStamp>,
        bus: UInt32,
        frameCount: UInt32
    ) -> OSStatus {
        let frames = Int(frameCount)
        guard frames > 0 else {
            return noErr
        }
        guard frames <= capacityFrames else {
            return kAudioUnitErr_TooManyFramesToProcess
        }
        var bufferList = AudioBufferList(
            mNumberBuffers: 1,
            mBuffers: AudioBuffer(
                mNumberChannels: UInt32(channelCount),
                mDataByteSize: UInt32(frames * channelCount * MemoryLayout<Float>.size),
                mData: UnsafeMutableRawPointer(renderBuffer)
            )
        )
        let status = AudioUnitRender(unit, flags, timeStamp, bus, frameCount, &bufferList)
        guard status == noErr else {
            return status
        }
        onSamples(Self.monoSamples(renderBuffer, frames: frames, channels: channelCount))
        return noErr
    }

    static func monoSamples(
        _ interleaved: UnsafePointer<Float>,
        frames: Int,
        channels: Int
    ) -> [Float] {
        guard channels > 1 else {
            return Array(UnsafeBufferPointer(start: interleaved, count: frames))
        }
        let divisor = Float(channels)
        var samples = [Float](repeating: 0, count: frames)
        for frame in 0..<frames {
            var sum: Float = 0
            for channel in 0..<channels {
                sum += interleaved[frame * channels + channel]
            }
            samples[frame] = sum / divisor
        }
        return samples
    }

    private static let inputElement: AudioUnitElement = 1
    private static let outputElement: AudioUnitElement = 0

    private static func check(_ status: OSStatus, _ step: String) throws {
        guard status == noErr else {
            throw AudioRecorderError.audioUnitFailed(step: step, status: status)
        }
    }
}

private let halInputCallback: AURenderCallback = { refCon, flags, timeStamp, bus, frameCount, _ in
    let capture = Unmanaged<HALInputCapture>.fromOpaque(refCon).takeUnretainedValue()
    return capture.render(flags: flags, timeStamp: timeStamp, bus: bus, frameCount: frameCount)
}
