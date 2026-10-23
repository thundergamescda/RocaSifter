"""Build the buyer-facing Roca Station operator manual (US Letter PDF)."""
from pathlib import Path
from html import escape
import json
import math
import sys
import tempfile
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph, Table, TableStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader

ROOT=Path(__file__).resolve().parents[1]
ASSETS=ROOT/'docs'/'manual-assets'
OUTPUT=ROOT/'docs'/'Roca_Station_Operator_Manual.pdf'
VERSION='2.1.0'
FONT_ROOT=Path('/usr/share/fonts/truetype/liberation')
if FONT_ROOT.exists():
    for name,file in [('Body','LiberationSans-Regular.ttf'),('Bold','LiberationSans-Bold.ttf'),('Italic','LiberationSans-Italic.ttf')]:
        pdfmetrics.registerFont(TTFont(name,str(FONT_ROOT/file)))
    pdfmetrics.registerFontFamily('Body',normal='Body',bold='Bold',italic='Italic',boldItalic='Bold')
else:
    from reportlab.pdfbase.pdfmetrics import Font
    for name,base in [('Body','Helvetica'),('Bold','Helvetica-Bold'),('Italic','Helvetica-Oblique')]:
        pdfmetrics.registerFont(Font(name,base,'WinAnsiEncoding'))
    pdfmetrics.registerFontFamily('Body',normal='Body',bold='Bold',italic='Italic',boldItalic='Bold')
NAVY=colors.HexColor('#142238');BLUE=colors.HexColor('#315DEA');INK=colors.HexColor('#1B2940')
MUTED=colors.HexColor('#63738B');LINE=colors.HexColor('#E2E8F1');PALE=colors.HexColor('#F2F5FC')
TEAL=colors.HexColor('#267A67');WARM=colors.HexColor('#A5692D');WHITE=colors.white
PAGE_W,PAGE_H=612,792;M=46;W=PAGE_W-2*M
PAGE_NAMES={
2:('desk','Your everyday three steps'),3:('import','Bring in a Roca export'),
4:('check','Check the physical cards'),5:('finish','Finish & print'),
6:('variants','Correct variants & copies'),7:('trade','Customer trade-ins'),
8:('bulk','Find value in bulk'),9:('online','Prepare online stock'),
10:('history','Find saved work'),11:('orders','Pull online orders'),
12:('admin','Admin & saved defaults'),13:('help','If something needs attention'),
14:('updates','Closing, updates & backups'),15:('store','Your store’s station profile')}


class Manual:
    def __init__(self):
        self.c=canvas.Canvas(str(OUTPUT),pagesize=(PAGE_W,PAGE_H),pageCompression=1)
        self.c.setTitle('Roca Station — Operator Manual | '+VERSION)
        self.c.setAuthor('Roca Station')
        self.c.setSubject('Operator workflows, trade-ins, Square receiving, QR labels, admin and troubleshooting')
        self.c.setKeywords('Roca Station, operator guide, Roca Sifter, Square, Zebra ZD411D, trade-ins, QR labels')
        self.c.setViewerPreference('DisplayDocTitle','true')
        self.c.showOutline()
        self.page=0;self.y=0;self.bounds=[]
    def check(self,y=None):
        y=self.y if y is None else y
        self.bounds.append((self.page,round(y,1)))
        if y<49:
            raise RuntimeError(f'Page {self.page} overflows: bottom {y:.1f}')
    def para(self,text,x=M,width=W,size=10.7,leading=15.2,color=INK,space=10,font='Body'):
        style=ParagraphStyle('p',fontName=font,fontSize=size,leading=leading,textColor=color,spaceAfter=0)
        p=Paragraph(text,style);_,h=p.wrap(width,700)
        p.drawOn(self.c,x,self.y-h);self.y-=h+space;self.check();return h
    def h2(self,text):
        self.y-=5;self.para(text,size=14.3,leading=19,font='Bold',space=8)
    def step(self,n,title,text):
        top=self.y
        self.c.setFillColor(PALE);self.c.circle(M+11,top-10,11,fill=1,stroke=0)
        self.c.setFillColor(BLUE);self.c.setFont('Bold',9.2);self.c.drawCentredString(M+11,top-13,str(n))
        self.para(title,x=M+34,width=W-34,size=11.2,leading=15.8,font='Bold',space=3)
        self.para(text,x=M+34,width=W-34,size=10.5,leading=14.7,space=12)
    def bullet(self,text):
        top=self.y;self.c.setFillColor(BLUE);self.c.circle(M+3,top-6,2,fill=1,stroke=0)
        self.para(text,x=M+16,width=W-16,size=10.5,leading=15.1,space=7)
    def callout(self,title,text,kind='note'):
        accent=TEAL if kind=='good' else WARM if kind=='care' else BLUE
        fill=colors.HexColor('#EEF7F3') if kind=='good' else colors.HexColor('#FFF6EB') if kind=='care' else PALE
        st=ParagraphStyle('call',fontName='Body',fontSize=10.2,leading=14.5,textColor=INK)
        a=Paragraph('<b>'+title+'</b>',st);b=Paragraph(text,st)
        _,ah=a.wrap(W-34,600);_,bh=b.wrap(W-34,600);h=ah+bh+31
        self.c.setFillColor(fill);self.c.roundRect(M,self.y-h,W,h,9,fill=1,stroke=0)
        self.c.setFillColor(accent);self.c.roundRect(M,self.y-h+10,3,h-20,1.5,fill=1,stroke=0)
        a.drawOn(self.c,M+17,self.y-13-ah);b.drawOn(self.c,M+17,self.y-19-ah-bh)
        self.y-=h+13;self.check()
    def image(self,name,cap='',max_h=270,width=W):
        path=ASSETS/name
        img=ImageReader(str(path));iw,ih=img.getSize();scale=min(width/iw,max_h/ih)
        dw,dh=iw*scale,ih*scale;x=M+(W-dw)/2
        self.c.setStrokeColor(LINE);self.c.setLineWidth(.6);self.c.roundRect(x-1,self.y-dh-1,dw+2,dh+2,5,fill=0,stroke=1)
        self.c.drawImage(img,x,self.y-dh,width=dw,height=dh,mask='auto')
        self.y-=dh+8
        if cap:self.para(cap,size=8.2,leading=11,color=MUTED,space=12)
        self.check()
    def table(self,headers,rows,widths=None,size=9.8):
        widths=widths or [W/len(headers)]*len(headers)
        style=ParagraphStyle('cell',fontName='Body',fontSize=size,leading=size+4,textColor=INK)
        head=ParagraphStyle('head',parent=style,textColor=WHITE,fontName='Bold')
        data=[[Paragraph(str(v),head) for v in headers]]+[[Paragraph(str(v),style) for v in row] for row in rows]
        table=Table(data,colWidths=widths,hAlign='LEFT')
        table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),NAVY),('ROWBACKGROUNDS',(0,1),(-1,-1),[WHITE,colors.HexColor('#F7F9FC')]),('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),11),('RIGHTPADDING',(0,0),(-1,-1),11),('TOPPADDING',(0,0),(-1,-1),9),('BOTTOMPADDING',(0,0),(-1,-1),9),('LINEBELOW',(0,0),(-1,0),.5,NAVY),('LINEBELOW',(0,1),(-1,-1),.4,LINE)]))
        _,height=table.wrap(W,700);table.drawOn(self.c,M,self.y-height);self.y-=height+13;self.check()
    def footer(self):
        self.c.setStrokeColor(LINE);self.c.setLineWidth(.6);self.c.line(M,38,PAGE_W-M,38)
        self.c.setFont('Body',8);self.c.setFillColor(MUTED);self.c.drawString(M,24,'ROCA STATION  /  OPERATOR MANUAL  /  '+VERSION)
        self.c.drawRightString(PAGE_W-M,24,f'{self.page:02d}')
    def new(self,page,kicker,title,subtitle='',role='OPERATOR'):
        if self.page:self.footer();self.c.showPage()
        self.page=page
        self.c.setFillColor(WHITE);self.c.rect(0,0,PAGE_W,PAGE_H,fill=1,stroke=0)
        self.c.setFillColor(BLUE);self.c.roundRect(M,746,19,20,5,fill=1,stroke=0)
        self.c.setFont('Bold',17);self.c.setFillColor(WHITE);self.c.drawString(M+5,750,'r')
        self.c.setFont('Bold',9);self.c.setFillColor(INK);self.c.drawString(M+28,753,'roca station')
        self.c.setFont('Body',7.5);self.c.setFillColor(MUTED);self.c.drawRightString(PAGE_W-M,753,role)
        self.c.setStrokeColor(LINE);self.c.line(M,736,PAGE_W-M,736)
        self.c.setFont('Bold',8);self.c.setFillColor(BLUE);self.c.drawString(M,713,kicker.upper())
        self.c.setFillColor(INK);self.c.setFont('Bold',26);self.c.drawString(M,678,title)
        self.y=658
        if subtitle:self.para(subtitle,size=11,leading=15.5,color=MUTED,space=17)
        key=PAGE_NAMES.get(page,(str(page),''))[0]
        self.c.bookmarkPage(key);self.c.addOutlineEntry(title,key,level=0)
    def finish(self):
        self.footer();self.c.save()
        (Path(tempfile.gettempdir())/'roca-manual-layout-check.json').write_text(json.dumps({'pages':self.page,'lowest_content_y':min(y for _,y in self.bounds),'version':VERSION},indent=2))

m=Manual();c=m.c
# 01 — Cover.
m.page=1;c.setFillColor(NAVY);c.rect(0,0,PAGE_W,PAGE_H,fill=1,stroke=0)
c.setFillColor(BLUE);c.roundRect(46,710,40,43,10,fill=1,stroke=0);c.setFillColor(WHITE);c.setFont('Bold',34);c.drawString(57,719,'r')
c.setFont('Bold',22);c.drawString(99,733,'roca');c.setFont('Body',8);c.setFillColor(colors.HexColor('#A6B9D7'));c.drawString(100,716,'S T A T I O N')
c.setFont('Bold',9);c.drawRightString(566,733,'OPERATOR EDITION')
c.setFillColor(WHITE);c.setFont('Bold',55);c.drawString(44,622,'The operator');c.drawString(44,561,'manual.')
c.setFillColor(colors.HexColor('#A8BDFB'));c.setFont('Body',21);c.drawString(47,519,'From scan to shelf.')
m.y=486;m.para('Three steps for everyday card processing.<br/>Extra tools for the jobs that need them.',size=12.8,leading=19,color=colors.HexColor('#CBD6E8'),space=14)
for i,word in enumerate(['IMPORT','CHECK','FINISH']):
    x=46+i*176;c.setFillColor(colors.HexColor('#213551'));c.roundRect(x,407,148,28,6,fill=1,stroke=0)
    c.setFillColor(WHITE);c.setFont('Bold',8.5);c.drawCentredString(x+74,417,word)
    if i<2:c.setFillColor(colors.HexColor('#7992C4'));c.drawString(x+158,417,'›')
img=ImageReader(str(ASSETS/'workspace.png'));iw,ih=img.getSize();dw=480;dh=dw*ih/iw
c.setFillColor(colors.HexColor('#2A3B58'));c.roundRect(63,64,486,dh+6,8,fill=1,stroke=0)
c.drawImage(img,66,67,width=dw,height=dh,mask='auto')
c.setFillColor(colors.HexColor('#91A6C6'));c.setFont('Body',8);c.drawString(46,49,'Illustrative training data shown. Prices are examples, not live market quotes.')
c.setFont('Bold',8);c.drawString(46,27,'VERSION '+VERSION);c.setFont('Body',8);c.drawRightString(566,27,'SQUARE RECEIVING  ·  ROCA EXPORTS  ·  ZEBRA LABELS')
c.bookmarkPage('cover');c.addOutlineEntry('Operator manual','cover',level=0);c.showPage();m.page=0
# 02 — Daily desk reference.
m.new(2,'Keep this by the station','Your everyday three steps','Use Process cards for the normal job. You choose whether to add stock after checking the cards.')
m.para('<b>Open:</b> double-click <b>Start Roca.vbs</b>. Chrome opens the station. Keep the physical stack together.',size=10.5)
m.step(1,'Import.','Click <b>Choose Roca export</b> and select the CSV you exported from Roca. Importing does not add stock.')
m.step(2,'Check cards.','Match name, set, number, condition, finish, quantity and price to the physical cards. Use <b>Edit</b> for corrections. Mark checked cards, then click <b>Continue</b>.')
m.step(3,'Finish.','Choose <b>Add to Square &amp; create labels</b> for new physical stock, or <b>Create labels only</b> when stock should not increase. Click the main button; approve any stock addition.')
m.callout('Print, then start fresh.','Click <b>Open labels to print</b>. Use 2″ × 1″ media at <b>Actual size / 100%</b>. Check one label, then print the stack. Click <b>Next batch</b> for a fresh import.','good')
m.para('<b>Reprints:</b> open <b>Batch history</b> and print the existing batch. A successful Square addition cannot be repeated from that batch.',size=10.3)
m.table(['When you need more','Where to find it'],[
('Buy cards from a customer','<b>Customer trade-in</b> · page 7'),
('Find valuable cards / prepare online files','<b>More tools</b> · pages 8–9'),
('Find, reprint or delete a batch','<b>Batch history</b> · page 10'),
('Printer, Square or export-folder settings','<b>Admin settings</b> · page 12'),
('An error / closing or updating','Pages 13–14')],[253,W-253],size=9.7)
m.para('Closing Chrome leaves Roca running. To stop it, save first and use <b>Stop Roca.vbs</b>. Updates: <b>Update Roca.vbs</b>.',size=9.8,leading=13.5,color=MUTED)
# 03 — Intake.
m.new(3,'Step 1 / Import','Bring in a Roca export','Every import starts as a draft. Nothing goes to Square until you choose and approve it.')
m.image('welcome.png','Process cards is the everyday starting point.',max_h=210)
m.step(1,'Export the scan in Roca.','Click Export in the Roca software and save its CSV. Give the file a recognizable name and keep its physical stack together.')
m.step(2,'Choose the file.','In <b>Process cards</b>, click <b>Choose Roca export</b>, or drop the CSV into the app. Check the batch title and included quantity.')
m.h2('If your manager sets up export pickup')
m.para('Export into the saved folder. Roca Station picks up stable CSV files as unchecked drafts. Open them from <b>Batch history</b>; an arriving file does not replace your current screen or add stock.')
m.callout('“This export is already saved.”','Open the existing batch for a retry or reprint. Choose the new-delivery override only for <b>additional physical cards</b>. Renaming a file does not make it new stock.','care')
m.para('Corrected or cumulative exports can overlap stock already received. Check for overlap before approval. The app reads exports; Roca still controls the physical sorting bins.',size=10.3,color=MUTED)
# 04 — Card review.
m.new(4,'Step 2 / Check cards','Check the physical cards','The review table comes first. Work through the stack before choosing what happens next.')
m.image('card-table.png','Search and filters check the whole batch; the table shows 40 rows per page.',max_h=240)
m.bullet('<b>Edit</b> opens a card. Verify its name, set, collector number, condition, exact finish, quantity and final retail price.')
m.bullet('Use the left checkbox to include or exclude a row. Only included cards count toward the next action.')
m.bullet('Mark the <b>Checked</b> box for cards you have verified. Use <b>Not checked yet</b> to find the remaining rows.')
m.bullet('<b>Mark included cards checked</b> asks you to confirm you physically checked all included cards. It does not assess condition for you.')
m.bullet('When every included row is checked, click <b>Continue</b>. Use <b>Back to cards</b> if anything needs another look.')
m.callout('Your work saves automatically.','Wait for <b>All changes saved</b> before leaving. <b>Batch options</b> also contains Save now, notes and a workflow switch. One operator should finish a batch at a time.','good')
# 05 — Finish.
m.new(5,'Step 3 / Finish','Finish & print','Make one clear choice for this batch. Each new batch starts with no inventory choice selected.')
m.image('finish.png','Square receiving is a deliberate choice after review.',max_h=230)
m.table(['Choose','Result'],[
('<b>Add to Square &amp; create labels</b>','Adds included incoming copies after confirmation, then creates the labels. Matching variants gain copies; their count is not reset.'),
('<b>Create labels only</b>','Creates labels without adding stock. Use for stock already counted, or when you only need printed labels.')],[208,W-208],size=10)
m.para('The finished screen reports the result and links to your labels. Click <b>Open labels to print</b>. <b>More files</b> holds ZPL and other documents. Files are stored in the app’s <b>outputs</b> folder.',size=10.3)
m.callout('For your Zebra ZD411D','Use <b>2″ wide × 1″ high</b> media, the printer’s actual 203/300 DPI and <b>Actual size / 100%</b>. Side fold puts card info and price left, QR right. Fold on a sleeve/toploader, keeping the QR flat on the back. Test one label first.','good')
m.para('A QR-capable scanner is required. Standard and top-fold layouts use Code 128. Admin saves the normal layout/DPI; <b>More options</b> changes them for this session. PDF/ZPL files are generated; the app does not send a job directly to the printer.',size=9.9,leading=14,color=MUTED)
# 06 — Variants.
m.new(6,'When a scan needs fixing','Correct variants & copies','Finish is free text: you can enter any current or future treatment.')
m.image('card-editor.png','The editor keeps everyday fields visible. Language, location and old-SKU overrides are under Other card details.',max_h=245)
m.step(1,'Correct the exact variant.','Use <b>Edit</b>. Check condition and enter the precise finish, such as Surge Foil, Etched Foil or another treatment. Suggestions are optional.')
m.step(2,'Confirm the price for that variant.','Changing identity or finish does not fetch a new market price. Look it up through your store’s pricing source, enter the final price and verify the card again.')
m.step(3,'Separate a different copy.','If a row has quantity three but one copy differs, choose <b>Separate one copy</b>. Edit that copy’s condition/finish/price. The original row drops to two; the total stays three.')
m.callout('After a committed action','An accepted buy or started receiving operation locks card identity, quantity and price. Notes and storage can still be updated. Correct completed stock in Square rather than rewriting the committed batch.','care')
# 07 — Trade-ins.
m.new(7,'Extra workflow','Customer trade-ins','Review the cards first, then agree on the offer. Buying and receiving are separate decisions.')
m.step(1,'Start a customer batch.','Choose <b>Customer trade-in</b>, import the export and check the cards as usual. Remove cards you will not buy, then click <b>Continue</b>.')
m.image('trade-offers.png','The offer uses included quantities, retail prices and the saved buying percentages.',max_h=200)
m.step(2,'Agree on cash or store credit.','Select the intended offer. If needed, expand <b>Adjust offer percentages</b>. Add a customer/collection reference and record the agreed offer using the main button.')
m.step(3,'Complete payment through your normal process.','The app records the buy and creates an acceptance PDF. It does not pay cash or issue store credit. Do not record an offer that the customer has not accepted.')
m.step(4,'Choose stock and labels.','After recording the accepted buy, choose whether to add the copies to Square and create labels, or make labels without adding stock. Finish the physical handoff and storage.')
m.para('Buying rates are store policy. The software’s defaults are editable examples, not recommended rates. The acceptance record is not a customer’s legal signature.',size=10.2,color=MUTED)
# 08 — Bulk.
m.new(8,'More tools','Find value in bulk','Use this when the question is “What is worth processing in this stack?”')
m.step(1,'Open the bulk tool.','Expand <b>More tools</b> in the left navigation and choose <b>Find value in bulk</b>. Import the Roca CSV.')
m.step(2,'Choose the value threshold.','In the review table’s <b>More options</b>, enter a minimum price and click <b>Apply filter</b>. Rows below that price are excluded; they are not deleted.')
m.step(3,'Review the selected stack.','Search for uncertain matches or premium treatments. Correct prices and include/exclude individual rows as needed. Compare quantities to the physical cards.')
m.step(4,'Continue processing the same batch.','Click the main continue button to move into <b>Process cards</b>. Check the included cards, continue to Finish and choose stock/labels there. You do not need to reimport.')
m.callout('A sift does not add inventory.','Filtering, reviewing, saving or moving a batch never adds Square stock. The value threshold uses the prices in your batch; no live repricing is connected. Physical bins remain configured in Roca.','good')
m.h2('Make the handoff easy')
m.para('Keep excluded cards separate from the selected stack. Put the batch ID with the selected cards and leave notes under <b>Batch options</b> for unusual treatments or items needing a second opinion.')
m.para('To move this same stack into a trade-in or online workflow, choose it under <b>Batch options → Continue this batch in</b>. Navigation starts a fresh import; Batch options moves the current batch.',size=10.5)
# 09 — Online.
m.new(9,'More tools','Prepare online stock','Prepare clear listing and storage information for cards kept in bins or in your display cases.')
m.step(1,'Choose Online preparation.','Expand <b>More tools</b>, open the online workflow, import and check the cards, then click <b>Continue</b>.')
m.image('online-storage.png','Both a dedicated online bin and display-case storage are supported.',max_h=225)
m.step(2,'Record where the cards will live.','Choose TCGplayer or eBay, select bin/display storage and enter a specific location. A location is required before receiving or preparing online stock.')
m.step(3,'Choose whether to add stock.','Add to Square and prepare files for new copies, or prepare files without stock changes. Read and approve any Square addition.')
m.step(4,'Use the files and store the cards.','Open the listing review CSV and storage ticket. Check the contents, publish through your verified seller workflow, and place the cards at the recorded location.')
m.callout('What these files do','The CSV is a generic review sheet, not a guaranteed TCGplayer/eBay import format. This release does not publish listings, sync marketplace stock, automatically delist sold cards or receive marketplace order notifications.','care')
# 10 — History.
m.new(10,'Saved work','Find saved work','Batch history keeps everyday work out of the way until you need it.')
m.image('history.png','Search the 150 most recent active batches, then click one to reopen it.',max_h=150)
m.table(['Task','How to do it'],[
('Start a fresh stack','Choose <b>Next batch</b> or <b>New batch</b>. Pending edits save; filters and the finish choice reset for the new import.'),
('Continue or reprint','Open <b>Batch history</b>. A finished batch opens its result. Print the PDF again, or choose <b>View this batch</b> to check it.'),
('Move this stack to another job','Use <b>Batch options → Continue this batch in</b>. The same saved record follows the cards.'),
('Delete a batch','Open it, choose <b>Batch options → Delete batch</b>, then confirm. It leaves the active history and returns to import.'),
('Restore a deleted batch','Reimport the same CSV, or open it from <b>Orders &amp; activity</b>, then accept restoration.')],[162,W-162],size=10)
m.callout('Deleting does not undo a transaction.','It does not remove Square stock, undo a buy or erase generated files. The source fingerprint and receiving journal are retained. A restored received batch cannot add copies again. Unresolved receiving must be resolved before deletion.','care')
# 11 — Pull tasks.
m.new(11,'More tools','Pull online orders','A manual task list helps the manager remove sold cards from their recorded locations.')
m.step(1,'Record the order.','Choose <b>More tools → Orders &amp; activity → Record online order</b>. Enter the channel, order reference, card/variant, quantity and storage location.')
m.image('pull-task.png','A precise case, shelf or bin location makes the task useful.',max_h=220)
m.step(2,'Physically pull and verify the cards.','Find the exact card, condition and treatment. Match quantity to the order and remove the cards from the display or bin.')
m.step(3,'Confirm the task only after removal.','Choose <b>Confirm pulled</b> and approve it. The task becomes PULLED. The activity section also links to batch events and deleted-batch restoration.')
m.callout('Manual coordination','Recording or completing a pull task does not change Square or marketplace stock. Orders must be entered manually, and stock/listings must be reconciled through your store’s normal sales process. Automatic shared inventory is not connected.','care')
# 12 — Admin.
m.new(12,'Manager setup','Admin & saved defaults','Set this up once so operators can focus on the cards.',role='ADMIN')
m.step(1,'Create or unlock Admin.','Open <b>Admin settings</b>. On first use, create your own password of at least 12 characters and confirm it. There is no default password. Closing the dialog locks it again.')
m.step(2,'Save the Square connection.','Enter the token, choose Production for the real store or Sandbox for a sandbox token, then <b>Test connection &amp; load stores</b>. Select the active receiving store and save. A blank token field keeps the saved credential.')
m.step(3,'Save normal printer settings.','Under <b>Workflow defaults</b>, choose the normal label layout and the ZD411D’s actual DPI. Operators can override them under Finish’s More options when necessary.')
m.step(4,'Choose optional defaults.','Set store-approved cash/credit percentages and sift threshold. To use export pickup, enter an existing folder on this computer and its default workflow. Leave the folder blank for manual import.')
m.image('admin-settings.png','The token field is never populated with the saved secret.',max_h=190)
m.para('Credentials are encrypted locally. Keep <b>station_data</b> private and use the same Windows account after updates/restores. Admin can change the password in the dialog. There is no browser password-reset bypass; do not erase station_data to reset access.',size=10.2,color=MUTED)
# 13 — Troubleshooting.
m.new(13,'When something needs attention','Read the message first','Keep the same batch for a receiving retry. Do not create a new delivery to bypass an error.')
m.table(['What you see','What to do'],[
('Continue is disabled','Include at least one card and mark every included row checked. On Finish, select an option. A trade-in must have its accepted buy recorded first.'),
('No stock appeared in Square','Check whether you chose labels/files only. The result reports confirmed units added. Buying, importing and printing alone do not add stock.'),
('Location not active / accessible','Ask Admin to test the token, match Production/Sandbox and select an active receiving store. Save and retry the same batch.'),
('CSV and Square prices differ','Confirm the intended retail price, then correct the batch or Square as appropriate. Existing Square prices are not silently changed.'),
('Receiving needs attention','Read the saved error. Use <b>Retry saved Square update</b> if appropriate. Persistent or old partial operations require checking Square before further action.'),
('QR will not scan / label is cut off','Use a fresh PDF, 2″ × 1″ media, correct DPI and 100% scaling. Keep the code and its white margins flat and clear. Verify your scanner reads QR.'),
('Old app / port is already in use','Save and use Stop Roca from the running folder. Close the old console once for a console-based version, then start the current folder.'),
('Password or installation trouble','Ask the manager who set the password. For startup errors, read station_data/station.log and rerun the current installer if prompted.')],[157,W-157],size=9.8)
m.callout('Useful support details','Record the version, batch ID, chosen action and exact error. Keep tokens and passwords out of screenshots. Test physical labels and a real Square variation sale on your store equipment before a full rollout.','note')
# 14 — Daily operation & maintenance.
m.new(14,'Keep work safe','Closing, updates & backups','Use the shortcuts in your installed app folder. Keep one working installation for the station.')
m.table(['Shortcut','Use it for'],[
('<b>Start Roca.vbs</b>','Starts or reopens Roca in Chrome without a command window. The station must be awake and signed in.'),
('<b>Stop Roca.vbs</b>','After saving, stops the station safely. Active stock work finishes first. Closing Chrome alone leaves Roca and export pickup running.'),
('<b>Update Roca.vbs</b>','Downloads the current release, backs up application files, installs dependencies and reopens Chrome. Save work and close the Roca browser first.')],[176,W-176],size=10.4)
m.h2('Update without copying app files by hand')
m.para('Open <b>Update Roca.vbs</b>, check the installation folder and choose <b>Update &amp; reopen Roca</b>. An older installation may ask you to close its console once. The updater preserves station_data, outputs and settings. Backups and a setup log are in the app’s <b>backups</b> folder.')
m.h2('Back up the business records')
m.para('Stop Roca and back up the complete <b>station_data</b> and <b>outputs</b> folders plus any legacy settings. Receipts, receiving journals, preferences, passwords and encrypted credentials live in station_data. Keep backups private. The Python .venv can be rebuilt; it is not part of the updater’s backup.')
m.callout('Keep history and the Windows account.','Do not delete station_data during an update. Losing receiving history also loses duplicate protection. Use the same Windows user so the credential can be decrypted; otherwise an admin must save a replacement token.','care')
m.para('<b>Where labels go:</b> use the batch’s file links, or open <b>outputs</b> beside the application. Generated files have unique names. Keep documents associated with their batch instead of choosing an older PDF by mistake.',size=10.4)
# 15 — Fillable station profile.
m.new(15,'Store handoff','Your store’s station profile','Fill this in with your manager. Keep credentials somewhere else.')
fields=[('Store / team name','store_name'),('Manager or admin contact','manager_contact'),('Station computer / Windows user','station_computer'),('Roca export folder','export_folder'),('Receiving Square store name','receiving_store'),('Zebra printer name / resolution','printer_details'),('Normal cash / credit offer policy','offer_policy'),('Display / bin naming convention','storage_convention')]
for label,name in fields:
    c.setFont('Bold',10.5);c.setFillColor(INK);c.drawString(M,m.y,label);m.y-=31
    c.acroForm.textfield(name=name,tooltip=label,x=M,y=m.y,width=W,height=25,fontName='Helvetica',fontSize=10,
                        borderStyle='solid',borderWidth=.6,borderColor=LINE,fillColor=colors.HexColor('#F8FAFD'),textColor=INK,forceBorder=True)
    m.y-=22
m.callout('Do not write a token or password on this page.','The admin token belongs in the app’s protected settings. Use your store’s approved process for sharing or recovering admin access.','care')
m.para('You can fill the fields in a compatible PDF reader or write on a printed copy. For the current project documentation, visit <link href="https://github.com/thundergamescda/RocaSifter" color="#315DEA">github.com/thundergamescda/RocaSifter</link>.',size=9.5,leading=13.5,color=MUTED)
m.finish()
# Publish the daily desk sheet and cover preview from the same source.
import fitz
with fitz.open(OUTPUT) as doc:
    quick=fitz.open();quick.insert_pdf(doc,from_page=1,to_page=1)
    quick.set_metadata({'title':'Roca Station — Operator Desk Reference','author':'Roca Station',
                        'subject':'Quick reference from Operator Manual '+VERSION+', page 2'})
    quick.save(str(ROOT/'docs'/'Roca_Station_Desk_Reference.pdf'),garbage=4,deflate=True)
    doc[0].get_pixmap(matrix=fitz.Matrix(1.25,1.25)).save(str(ASSETS/'manual-cover.png'))
print('Built',OUTPUT)
