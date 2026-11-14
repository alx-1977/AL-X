// The words in a picture, with their places, by macOS's own text recognition
// (Vision). Built once from this source by picture_text.py and always run
// confined. Usage: picture-text <image.png>
// Prints one line per word: left, top, width, height (pixels, from the top
// left) and the word, separated by tabs.
import AppKit
import Foundation
import Vision

guard CommandLine.arguments.count == 2,
      let image = NSImage(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1])),
      let picture = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    FileHandle.standardError.write("not a readable image\n".data(using: .utf8)!)
    exit(2)
}
let width = Double(picture.width), height = Double(picture.height)
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
// Dimensions are not words: no dictionary may turn 1.20 into something else.
request.usesLanguageCorrection = false
do {
    try VNImageRequestHandler(cgImage: picture, options: [:]).perform([request])
} catch {
    FileHandle.standardError.write("recognition failed\n".data(using: .utf8)!)
    exit(3)
}
for observation in request.results ?? [] {
    guard let line = observation.topCandidates(1).first else { continue }
    var from = line.string.startIndex
    for word in line.string.split(separator: " ") {
        guard let range = line.string.range(of: String(word), range: from..<line.string.endIndex) else { continue }
        from = range.upperBound
        let box = ((try? line.boundingBox(for: range)) ?? nil)?.boundingBox ?? observation.boundingBox
        print(String(format: "%.1f\t%.1f\t%.1f\t%.1f\t%@", box.minX * width, (1 - box.maxY) * height,
                     box.width * width, box.height * height, String(word)))
    }
}
