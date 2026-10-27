// A minimal PDF writer for the preview suite, so every fixture document is generated from a
// readable description instead of checked in as an opaque binary.
//
// Each page is US Letter with a large filled band in its own colour (what the spec samples from the
// rendered canvas to prove *which* page was drawn) and a "Page N" label. ``cjk`` adds a line set in
// a non-embedded Adobe-GB1 font through the predefined ``UniGB-UCS2-H`` CMap — the shape of PDFs
// many generators emit for Chinese text, and one pdf.js can only decode with its bundled CMaps.

export type Rgb = readonly [number, number, number];

export const PAGE_WIDTH = 612;
export const PAGE_HEIGHT = 792;
// The coloured band, in PDF user space (origin bottom-left).
export const BAND = { x: 56, y: 240, width: 500, height: 320 } as const;

export function pageColor(index: number): Rgb {
  const palette: Rgb[] = [
    [220, 40, 40],
    [40, 170, 60],
    [40, 80, 220],
    [230, 160, 20],
    [150, 50, 200],
    [20, 170, 190],
  ];
  return palette[index % palette.length];
}

const unit = (v: number) => (v / 255).toFixed(3);

export function buildPdf({ pages, cjk = false }: { pages: number; cjk?: boolean }): Buffer {
  const objects: string[] = [];
  const add = (body: string) => {
    objects.push(body);
    return objects.length;
  };

  const catalog = add('');
  const pagesNode = add('');
  const helvetica = add('<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>');
  let song = 0;
  if (cjk) {
    const descriptor = add(
      '<< /Type /FontDescriptor /FontName /STSong-Light /Flags 6 /FontBBox [-25 -254 1000 880] /ItalicAngle 0 /Ascent 880 /Descent -120 /CapHeight 880 /StemV 93 >>',
    );
    const cidFont = add(
      `<< /Type /Font /Subtype /CIDFontType0 /BaseFont /STSong-Light /CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) /Supplement 4 >> /FontDescriptor ${descriptor} 0 R /DW 1000 >>`,
    );
    song = add(`<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light-UniGB-UCS2-H /Encoding /UniGB-UCS2-H /DescendantFonts [${cidFont} 0 R] >>`);
  }

  const kids: number[] = [];
  for (let i = 0; i < pages; i += 1) {
    const [r, g, b] = pageColor(i);
    const ops = [
      `${unit(r)} ${unit(g)} ${unit(b)} rg ${BAND.x} ${BAND.y} ${BAND.width} ${BAND.height} re f`,
      `0 0 0 rg BT /F1 48 Tf 72 680 Td (Page ${i + 1}) Tj ET`,
    ];
    // "中文预览" as UCS-2 code units.
    if (cjk) ops.push('0 0 0 rg BT /F2 40 Tf 72 620 Td <4E2D6587987889C8> Tj ET');
    const stream = ops.join('\n');
    const content = add(`<< /Length ${Buffer.byteLength(stream)} >>\nstream\n${stream}\nendstream`);
    const fonts = `/F1 ${helvetica} 0 R${cjk ? ` /F2 ${song} 0 R` : ''}`;
    kids.push(
      add(`<< /Type /Page /Parent ${pagesNode} 0 R /MediaBox [0 0 ${PAGE_WIDTH} ${PAGE_HEIGHT}] /Contents ${content} 0 R /Resources << /Font << ${fonts} >> >> >>`),
    );
  }
  objects[catalog - 1] = `<< /Type /Catalog /Pages ${pagesNode} 0 R >>`;
  objects[pagesNode - 1] = `<< /Type /Pages /Kids [${kids.map((k) => `${k} 0 R`).join(' ')}] /Count ${kids.length} >>`;

  let out = '%PDF-1.4\n';
  const offsets: number[] = [];
  objects.forEach((body, i) => {
    offsets.push(Buffer.byteLength(out));
    out += `${i + 1} 0 obj\n${body}\nendobj\n`;
  });
  const xref = Buffer.byteLength(out);
  out += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  for (const offset of offsets) out += `${String(offset).padStart(10, '0')} 00000 n \n`;
  out += `trailer\n<< /Size ${objects.length + 1} /Root ${catalog} 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(out, 'latin1');
}
