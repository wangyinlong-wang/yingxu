import AppKit
import CoreImage
import Foundation

let payload = FileHandle.standardInput.readDataToEndOfFile()
guard !payload.isEmpty, let filter = CIFilter(name: "CIQRCodeGenerator") else {
    exit(1)
}
filter.setValue(payload, forKey: "inputMessage")
filter.setValue("M", forKey: "inputCorrectionLevel")
guard let image = filter.outputImage else {
    exit(1)
}
let enlarged = image.transformed(by: CGAffineTransform(scaleX: 8, y: 8))
let context = CIContext()
guard let bitmap = context.createCGImage(enlarged, from: enlarged.extent),
      let png = NSBitmapImageRep(cgImage: bitmap).representation(using: .png, properties: [:]) else {
    exit(1)
}
FileHandle.standardOutput.write(png)
