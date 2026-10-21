param([string]$Image, [string]$Lang = "ru")
# Windows' own OCR (Windows.Media.Ocr): local, no install. Prints one line per recognised line:
# text <TAB> x <TAB> y <TAB> w <TAB> h   (pixels of the image)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$t) { $m = $asTask.MakeGenericMethod($t); $task = $m.Invoke($null, @($op)); $task.Wait(-1) | Out-Null; $task.Result }
$langs = [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | ForEach-Object { $_.LanguageTag }
Write-Output ("#langs " + ($langs -join ","))
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Image)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$dec = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bmp = Await ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new($Lang))
if ($engine -eq $null) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
Write-Output ("#max " + [Windows.Media.Ocr.OcrEngine]::MaxImageDimension)
$res = Await ($engine.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])
foreach ($line in $res.Lines) {
    $xs = $line.Words | ForEach-Object { $_.BoundingRect }
    $x0 = ($xs | Measure-Object -Property X -Minimum).Minimum
    $y0 = ($xs | Measure-Object -Property Y -Minimum).Minimum
    $x1 = ($xs | ForEach-Object { $_.X + $_.Width } | Measure-Object -Maximum).Maximum
    $y1 = ($xs | ForEach-Object { $_.Y + $_.Height } | Measure-Object -Maximum).Maximum
    Write-Output ("{0}`t{1}`t{2}`t{3}`t{4}" -f $line.Text, [int]$x0, [int]$y0, [int]($x1 - $x0), [int]($y1 - $y0))
}
