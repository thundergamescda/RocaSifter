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
VERSION='2.0.4'
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
3:('quick-start','Start your day'),4:('workspace','Know your workspace'),5:('choose','Choose the right workflow'),
6:('import','Bring in a Roca export'),7:('review','Review a card'),8:('split','Handle different copies'),
9:('bulk','Bulk scan / sift'),10:('trade','Walk-in trade-ins'),11:('price','Price & label'),
12:('fold','Your side-fold QR label'),13:('print','Print and reprint'),14:('online','Online preparation'),
15:('orders','Activity & orders'),16:('inventory','Keep inventory accurate'),17:('admin','First-time setup & Admin'),
18:('defaults','Export pickup & defaults'),19:('files','Files, closing & backups'),20:('troubleshooting','If something goes wrong'),
21:('square-help','Square & label troubleshooting'),22:('desk','One-page desk reference'),23:('store','Your store’s station profile')}

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
m.y=486;m.para('A clear guide to reviewing cards, recording buys,<br/>receiving stock and creating shelf-ready labels.',size=12.8,leading=19,color=colors.HexColor('#CBD6E8'),space=14)
for i,word in enumerate(['IMPORT','REVIEW','RECEIVE','LABEL']):
    x=46+i*136;c.setFillColor(colors.HexColor('#213551'));c.roundRect(x,407,112,28,6,fill=1,stroke=0)
    c.setFillColor(WHITE);c.setFont('Bold',8.5);c.drawCentredString(x+56,417,word)
    if i<3:c.setFillColor(colors.HexColor('#7992C4'));c.drawString(x+120,417,'›')
img=ImageReader(str(ASSETS/'workspace.png'));iw,ih=img.getSize();dw=480;dh=dw*ih/iw
c.setFillColor(colors.HexColor('#2A3B58'));c.roundRect(63,64,486,dh+6,8,fill=1,stroke=0)
c.drawImage(img,66,67,width=dw,height=dh,mask='auto')
c.setFillColor(colors.HexColor('#91A6C6'));c.setFont('Body',8);c.drawString(46,49,'Illustrative training data shown. Prices are examples, not live market quotes.')
c.setFont('Bold',8);c.drawString(46,27,'VERSION '+VERSION);c.setFont('Body',8);c.drawRightString(566,27,'SQUARE RECEIVING  ·  ROCA EXPORTS  ·  ZEBRA LABELS')
c.bookmarkPage('cover');c.addOutlineEntry('Operator manual','cover',level=0);c.showPage();m.page=0
# 02 — Contents.
m.new(2,'Start here','Find the right page.','Start with the daily quick start. Use the rest of this manual when a task or question comes up.')
groups=[('GET STARTED',[3,4,5,6]),('WORK THE CARDS',[7,8,9,10,11]),('LABEL & PREPARE',[12,13,14,15]),('KEEP CONTROL',[16,17,18,19]),('HELP & DESK REFERENCE',[20,21,22,23])]
positions=[(46,620),(46,480),(46,310),(324,620),(324,445)]
for (label,pages),(x,y) in zip(groups,positions):
    c.setFont('Bold',8);c.setFillColor(BLUE);c.drawString(x,y,label);y-=24
    for page in pages:
        key,title=PAGE_NAMES[page];c.setFont('Body',10);c.setFillColor(INK);c.drawString(x,y,title)
        c.setFont('Bold',9);c.setFillColor(BLUE);c.drawRightString(x+242,y,f'{page:02d}')
        c.linkRect('',key,(x,y-5,x+242,y+13),relative=0,thickness=0)
        c.setStrokeColor(LINE);c.line(x,y-10,x+242,y-10);y-=27
m.y=163;m.callout('One receipt follows the cards.','Use <b>Move receipt to…</b> to continue a stack in another workflow. You do not need to import it again.','good')
m.para('For Roca Station '+VERSION+' on a local Windows workstation. Screenshots use fictional training receipts. Labels, buy records and listing sheets are created only when you choose their actions.',size=9.2,leading=13,color=MUTED)
# 03 — Daily start.
m.new(3,'Daily quick start','Start your day.','The short version: open, import, review, choose an action, then check the result.')
m.step(1,'Open the station.','Double-click <b>Start Roca.vbs</b> (or <b>Start Roca.cmd</b>). Roca runs in the background without a command window. Check Chrome shows version <b>'+VERSION+'</b>.')
m.step(2,'Choose the job before importing.','Select a workflow tab. Each tab opens a fresh import screen; reopen saved work from the receipt menu.')
m.step(3,'Export from Roca.','Click Export in Roca. Save to the watched export folder, or click <b>New batch</b> → <b>Choose a Roca export</b>.')
m.step(4,'Review the included cards.','Correct identity, set, condition, finish, quantity and price. Confirm only cards you have actually checked.')
m.step(5,'Choose the action you intend.','Receive new copies, print labels only, record an accepted buy, or prepare listing files. Read the action’s confirmation before continuing.')
m.step(6,'Check and finish the physical work.','Check the receipt status and file links. Print at 100%, attach labels to sleeves, and put the cards in their recorded locations.')
m.callout('Need only another label?','Open the existing receipt and use <b>Labels only</b> or <b>Create QR labels</b>. Do not import a new delivery to reprint.','care')
m.h2('Three useful words')
m.para('<b>Receipt:</b> a saved record for one physical batch. <b>CSV:</b> the file exported by Roca. <b>SKU:</b> the code identifying a particular card variant in Square.',size=10.5)
# 04 — Workspace map.
m.new(4,'The workspace','Know your workspace.','The same layout follows you through all four workflows.')
m.image('workspace.png','The workspace: workflow navigation on the left; receipt controls and review information on the right.',max_h=255)
m.table(['Area','What it is for'],[
('Left navigation','Choose a workflow. Open <b>Activity & orders</b> or password-protected <b>Admin settings</b>.'),
('Receipt menu + title','Reopen a saved receipt. Give a stack a useful name and keep its receipt ID with the physical cards.'),
('Three summary cards','See included copies, proposed retail value and review progress.'),
('Review table + bottom actions','Edit the cards, then choose whether to receive stock, create labels or prepare a listing batch.')],[138,W-138],size=10)
m.para('<b>Save</b> or <b>Ctrl+S</b> saves your receipt. Routine edits also save automatically. Wait for <b>All changes saved</b> before closing or handing over the station.',size=10.5)
# 05 — Workflow/action selection.
m.new(5,'Choose your path','Choose the right workflow.','Pick the job that matches the physical stack in front of you.')
m.table(['Choose this tab','When to use it'],[
('<b>Bulk scan / sift</b>','You are exploring a collection and deciding which cards deserve more processing.'),
('<b>Walk-in trade-ins</b>','You need a reviewed cash/store-credit offer before buying the cards.'),
('<b>Price & label</b>','The stack is ready for final price review, receiving and retail labels.'),
('<b>Online preparation</b>','You want a listing review sheet and a storage ticket for a bin or display case.')],[165,W-165],size=10.5)
m.h2('Understand the action before you click.')
m.table(['Action','Changes Square stock?'],[
('Import, review, save, or move a receipt','<b>No.</b> These organize the work.'),
('Record accepted cash / credit offer','<b>No.</b> Records the agreed buy; does not pay or issue credit.'),
('Labels only / Create QR labels','<b>No.</b> Creates files for printing.'),
('Receive & create labels / Receive & prepare batch','<b>Yes.</b> Adds the included incoming copies after approval.'),
('Prepare only / Prepare listing batch','<b>No</b> when used for already-owned or already-received stock. Creates listing files only.')],[268,W-268],size=10)
m.para('<b>New batch</b> and workflow tabs return to a fresh import screen. Saved receipts stay in the menu. <b>Move receipt to…</b> continues the same receipt in another workflow.',size=10.5)
# 06 — Intake.
m.new(6,'Import & export pickup','Bring in a Roca export.','Every import begins as a draft. It never accepts a buy or receives stock automatically.')
m.step(1,'Export the scan in Roca.','Choose a CSV export. Keep each physical stack together and use a recognizable file name.')
m.step(2,'Use manual import or folder pickup.','Click <b>New batch</b> → <b>Choose a Roca export</b>, or drop a CSV. Choose <b>Add new copies</b> or <b>Do not add inventory</b> for this batch. Bulk sift asks when moved to a receiving workflow.')
m.step(3,'If folder pickup is configured, wait for the draft.','Export into the folder chosen by your admin. Open the draft from the saved receipt menu; it will not replace your current view. Choose its inventory option when prompted.')
m.step(4,'Match the receipt to the physical stack.','Check its title, source filename, included copies and receipt ID before reviewing or labeling.')
m.callout('“This export is already saved.”','Open the existing receipt. Choose <b>This is a new delivery</b> only when the file represents genuinely additional physical copies—not a reprint, retry or renamed export.','care')
m.h2('Corrected and cumulative exports need a check.')
m.para('A revised export can contain cards already received. If the contents have changed, the app may create another draft. Review for overlap before receiving; a new filename is not proof of new stock.')
m.para('The app reads Roca exports. You still click Export in Roca, and physical sorting bins are configured in Roca.',size=10.4,color=MUTED)
# 07 — Card editor, side-by-side.
m.new(7,'Review first','Review a card.','Correct the record before money, inventory or labels depend on it.')
img=ImageReader(str(ASSETS/'card-editor.png'));iw,ih=img.getSize();dw=218;dh=min(454,dw*ih/iw);dw=dh*iw/ih
c.drawImage(img,M,m.y-dh,width=dw,height=dh,mask='auto')
start_y=m.y;m.y=start_y
right=M+242;rw=W-242
for title,text in [
('1  Open the editor.','Click the pencil at the right of a card row.'),
('2  Verify identity.','Check the name, set, collector number and language against the physical card.'),
('3  Verify condition & finish.','Type the exact treatment. The suggestions are optional; custom finishes are allowed.'),
('4  Confirm quantity & price.','Use the final retail price for this exact variant and condition.'),
('5  Mark it reviewed.','Check the verification box, then click <b>Save card</b>.')]:
    m.para(title,x=right,width=rw,size=11,leading=15,font='Bold',space=4)
    m.para(text,x=right,width=rw,size=10.3,leading=14.5,space=14)
m.y=min(m.y,start_y-dh)-16;m.check()
m.callout('A finish change does not fetch a new price.','Changing Foil to Surge Foil clears the review confirmation. Look up and enter the correct price using your store’s pricing source, then confirm the card again.','care')
# 08 — Copies and review.
m.new(8,'Review quantities','Handle different copies.','Group identical cards. Split copies when their condition or treatment is different.')
m.h2('Example: three copies are not all the same.')
m.para('A scan row says <b>3 × Near Mint / Foil</b>. Physical review finds two regular foils and one surge foil.')
m.step(1,'Open the row and choose Split one copy.','One copy moves into a separate row. The original row’s quantity drops by one; the total remains three.')
m.step(2,'Correct the separated copy.','Enter <b>Surge Foil</b>, check condition, and set its correct price. Mark that row reviewed and save it.')
m.step(3,'Review the remaining copies.','Confirm that the original row’s quantity of two is correct and that both copies match its recorded condition and finish.')
m.table(['Before review','After review'],[
('3 × Foil / Near Mint','2 × Foil / Near Mint<br/>1 × Surge Foil / Near Mint')],[W/2,W/2],size=11)
m.h2('Use the table to work efficiently.')
m.bullet('The checkbox includes or excludes a row from the next action. It does not delete the card record.')
m.bullet('Search by name, set, finish or SKU. Use <b>Needs review</b> to find unfinished rows.')
m.bullet('The table shows 40 rows per page. Search and filters check the entire receipt.')
m.bullet('Use <b>Confirm included reviewed</b> only after checking every included card. It is an approval, not an automatic condition assessment.')
# 09 — Bulk.
m.new(9,'Workflow 01','Bulk scan / sift','Find the cards worth processing without creating stock by accident.')
m.step(1,'Choose Bulk scan / sift and import.','The workspace brings in card rows from all export bins. Compare the stack with the imported quantities.')
m.step(2,'Apply your value threshold.','Enter the minimum beside <b>Include ≥ $</b>, then click <b>Apply</b>. This selects rows using their proposed retail prices.')
m.step(3,'Check the results.','Search for premium variants, correct questionable matches, and include or exclude rows as needed.')
m.step(4,'Save the receipt.','Use a title that identifies the collection or sorting batch. Add notes for the next person.')
m.step(5,'Continue with the same receipt.','Choose <b>Continue to price & label</b>, or use <b>Move receipt to…</b> to build a trade-in offer or prepare online stock.')
m.callout('Bulk sift does not receive inventory.','The threshold changes which rows are included in the app. It does not command the Roca machine, change physical bins or add cards to Square.','good')
m.h2('A useful handoff')
m.para('Keep the receipt ID with the stack. Leave clear notes about unusual treatments, uncertain matches or cards needing a second opinion. Keep excluded cards physically separate from cards chosen for the next step.')
# 10 — Trade-ins.
m.new(10,'Workflow 02','Walk-in trade-ins','Build an informed offer, then record the accepted buy.')
m.image('trade-offers.png','Cash and store-credit offers use included quantities, proposed retail prices and your chosen percentages.',max_h=136)
m.step(1,'Import and review the customer’s cards.','Correct identities, conditions, finishes, quantities and retail prices. Add a customer or collection reference if useful.')
m.step(2,'Set the offer.','Adjust the cash and store-credit percentages according to store policy. Select the offer card you intend to use.')
m.step(3,'Agree with the customer before recording acceptance.','Click <b>Record accepted cash offer</b> or <b>Record accepted credit offer</b>, check the confirmation, then record the buy.')
m.step(4,'Handle payment separately.','Pay the customer or issue store credit through the store’s normal process. The app does not transfer money or issue credit.')
m.step(5,'Receive and label the purchased cards.','Use <b>Receive & create labels</b> when ready to add these incoming copies to Square. Open the accepted-buy receipt and label files as needed.')
m.callout('Acceptance locks the reviewed card details.','Finish the physical review before recording the buy. If the customer declines, leave the offer unaccepted; nothing has been paid or received.','care')
# 11 — Known stack.
m.new(11,'Workflow 03','Price & label','Choose whether these are additional copies or cards already in stock.')
m.table(['Add new copies','Do not add inventory'],[
('Use when these physical copies have not yet been added to Square.','Use for existing stock that needs labels or a new label run.'),
('<b>Receive & create labels</b> adds included copies to Square, then creates label files after confirmation.','<b>Create QR labels</b> or <b>Labels only</b> creates files without adding stock.')],[W/2,W/2],size=10.7)
m.step(1,'Open the right receipt.','Move an existing receipt here, or import a genuinely new stack. Choose separately for each batch; <b>Decide later</b> leaves receiving disabled.')
m.step(2,'Review final prices and variants.','Check quantity, condition and finish. Mark every included card reviewed.')
m.step(3,'Choose the label layout and DPI.','Use <b>Side fold · QR right</b> for the left-info/right-code design. Select the actual 203 or 300 DPI of your Zebra.')
m.step(4,'Create the files and check the result.','For receiving, read the confirmation and wait for <b>RECEIVED</b>. Then open the label PDF from the file links below the actions.')
m.callout('Existing Square prices are not automatically changed.','Editing the receipt price changes the proposed label price. For existing stock, ensure the Square price matches before printing. Receiving stops if the incoming price conflicts with the existing Square price. For older stock, verify the existing SKU before changing labels (page 21).','care')
# 12 — Label anatomy.
m.new(12,'Label guide','Your side-fold QR label','Card information on the front. The matching SKU on the back.')
m.y-=4
m.image('side-label.png','Enlarged training label. Use the label PDF from the app—not this manual—to print retail labels.',max_h=225,width=490)
# Fold marks outside the label image, never through the printed QR.
y=m.y+20;c.setStrokeColor(BLUE);c.setLineWidth(1);c.setDash(3,3);c.line(PAGE_W/2,y+25,PAGE_W/2,y+236);c.setDash()
m.table(['Left half → front of sleeve','Right half → back of sleeve'],[
('Name, set / number, condition, finish and price.','QR code and human-readable SKU. Keep the code and its white margin flat.')],[W/2,W/2],size=10.7)
m.step(1,'Fold vertically through the center of the label.','A 2″ wide × 1″ high label becomes two 1″ × 1″ faces. Place the fold over the side edge of the protective sleeve.')
m.step(2,'Keep the QR code clear and flat.','Do not fold through it or cover its white margin. Attach adhesive to the sleeve or holder, not directly to the collectible card.')
m.callout('A QR-capable scanner is required.','A 1D laser scanner cannot read this layout. Test one physical label with your actual Square register. Standard and top-fold layouts remain available for linear barcodes.','care')
# 13 — Printing.
m.new(13,'Printing','Print and reprint','A label run is separate from a stock receipt.')
m.step(1,'Open the new label PDF.','Use <b>Open label PDF</b> below the receipt actions. Generated files are also in the application’s <b>outputs</b> folder.')
m.step(2,'Choose the Zebra ZD411D.','Use direct thermal stock measuring <b>2 inches wide × 1 inch high</b>. Match the driver’s stock size and the printer’s actual DPI.')
m.step(3,'Print at Actual size / 100%.','Turn off fit-to-page scaling. Print one label first; check orientation, readable text and a complete QR code or barcode.')
m.step(4,'Scan-test the first label.','Verify that the actual checkout scanner selects the correct Square item variation. A code that looks correct is not enough.')
m.step(5,'Print and attach the remaining labels in order.','Keep the physical stack aligned with the receipt. One label is generated for each included copy.')
m.h2('A jam or missing label does not need another receipt.')
m.para('Open the saved receipt and use <b>Labels only</b> or <b>Create QR labels</b>. If the PDF viewer supports page ranges, print only the missing pages from the existing PDF.')
m.callout('PDF or ZPL?','The PDF is the normal view-and-print route. <b>Download Zebra ZPL</b> is for a separately configured raw-print workflow. This version does not send jobs directly to a USB or wireless printer.','note')
# 14 — Online preparation.
m.new(14,'Workflow 04','Online preparation','Prepare the listing information without losing track of the physical cards.')
m.image('online-storage.png','Choose a sales channel, physical storage type and default location.',max_h=142)
m.step(1,'Review the cards and choose their storage.','Select TCGplayer or eBay. Choose <b>Dedicated online bin</b> or <b>Display case / shop floor</b>. Enter a specific location, such as “Case 2, shelf B.”')
m.step(2,'Use row-specific locations when needed.','A location in a card’s editor overrides the receipt’s default location for that row.')
m.step(3,'Choose whether to receive incoming copies.','For new stock, <b>Receive & prepare batch</b> receives it in Square first. For existing stock, choose <b>Do not add inventory</b>.')
m.step(4,'Create the preparation files.','Use <b>Prepare listing batch</b> or <b>Prepare only</b>. Download the review CSV and open the storage ticket. Retail labels are a separate action.')
m.callout('Preparation is not publication.','The CSV is a generic review sheet, not a guaranteed TCGplayer/eBay import format. Publish through your approved seller workflow. This app does not list, delist or synchronize marketplace quantities automatically.','care')
# 15 — Activity and pulls.
m.new(15,'Fulfillment','Activity & orders','A clear record of what happened—and which physical cards need to move.')
m.image('pull-task.png','Example manual pull task. The storage location tells the operator where to find the card.',max_h=116)
m.step(1,'Record an online order when it arrives.','Open <b>Activity & orders</b>, then <b>Record online order</b>. This is manual entry; marketplace notifications are not connected.')
m.step(2,'Enter enough information to pull the correct copy.','Record channel, order reference, card / variant / condition, quantity and exact storage location. Add notes if useful.')
m.step(3,'Remove the requested cards from sale.','Find the copies, verify the treatment and condition, and move them from the display or bin into the order’s holding area.')
m.step(4,'Confirm the physical pull.','Click <b>Confirm pulled</b> only after the copies have actually been removed and assigned to the order.')
m.callout('Confirm pulled does not change inventory.','Handle sale-related stock updates through your store’s current channel process. This app does not automatically reconcile an online sale against Square or other marketplaces.','care')
m.h2('Use receipt history to answer “what happened?”')
m.para('The history records imports, saves, accepted buys, receiving results, label runs and prepared listing files. Click a receipt ID to reopen that receipt. History is stored locally on this station.')
# 16 — Inventory controls.
m.new(16,'Receiving accurately','Keep inventory accurate.','The difference between another copy and another submission matters.')
m.table(['Situation','Correct handling'],[
('The same card arrives again','Use a genuinely new receipt. A matching SKU receives an <b>additional</b> quantity.'),
('The same CSV was imported twice','Open the existing receipt. Do not use the new-delivery override for the same physical cards.'),
('You need more labels','Reopen the receipt and print labels only. No additional stock is needed.'),
('A receipt says RECEIVED','That receipt’s stock is already committed. Moving or reopening it does not receive it twice.')],[170,W-170],size=10.2)
m.h2('Example: receiving two more copies')
c.setFillColor(PALE);c.roundRect(M,m.y-74,W,74,9,fill=1,stroke=0)
for x,number,label in [(M+70,'4','ALREADY IN STOCK'),(M+260,'2','INCOMING COPIES'),(M+450,'6','RESULTING STOCK')]:
    c.setFillColor(INK if number!='6' else TEAL);c.setFont('Bold',29);c.drawCentredString(x,m.y-34,number)
    c.setFont('Bold',7.2);c.setFillColor(MUTED);c.drawCentredString(x,m.y-55,label)
c.setFont('Body',22);c.drawCentredString(M+164,m.y-34,'+');c.drawCentredString(M+355,m.y-34,'=');m.y-=89
m.para('This example assumes no intervening sales. The app adds incoming copies; it does not reset the existing quantity to two.')
m.h2('If receiving is interrupted')
m.para('Open the <b>same receipt</b>, read its error and use <b>Retry saved receiving</b> when offered. The app reuses the saved requests. Restore the original Square environment and location before retrying.')
m.para('<b>Delete:</b> removes a receipt from the list, keeping stock and receiving history. Reimport its CSV or open its Activity entry to restore. Resolve receiving errors first.',size=10.3,leading=14,space=6)
m.callout('Do not create another receipt to bypass an error.','A partial or uncertain operation may already have changed Square. If the error persists or the request is old, stop and ask an admin to check Square before further action.','care')
# 17 — Admin setup.
m.new(17,'Administrator setup','First-time setup & Admin','An admin sets the connection once. Operators then use it without seeing the token.',role='ADMIN')
m.step(1,'Install the complete application folder.','Use Python 3.10 or newer; 64-bit Python 3.12 or 3.13 is recommended. Extract the complete ZIP, run <b>Install dependencies.cmd</b>, then <b>Start Roca.cmd</b>.')
m.step(2,'Create your admin password.','Open <b>Admin settings</b>. Enter at least 12 characters, confirm it, and choose <b>Create admin password</b>. There is no default password.')
m.image('admin-setup.png','First-time setup asks for a new password. Later visits show Unlock Admin.',max_h=165,width=360)
m.step(3,'Enter the Square connection.','Enter the token in Admin. Select <b>Production · real store</b> for live stock or <b>Sandbox · test store</b> for testing. A token must belong to that environment.')
m.step(4,'Select and save the receiving store.','Click <b>Test connection & load stores</b>, verify the store, then <b>Save settings</b>. A blank token field keeps an already saved token.')
m.callout('Close Admin when finished.','Closing the dialog locks it. Operators do not need the token or admin password to work with the saved connection. If Unlock Admin is shown, a password already exists.','good')
# 18 — Admin defaults.
m.new(18,'Administrator settings','Export pickup & defaults','Configure the station once, then keep daily work focused.',role='ADMIN')
m.step(1,'Create a dedicated export folder.','Choose a folder on this computer, such as <b>Documents / Roca Exports</b>. Configure Roca to export there when you click Export.')
m.step(2,'Save the folder in Admin.','Under <b>Automatic export pickup</b>, enter the full existing folder path and choose the default workflow. Click <b>Save settings</b>. Leave the path blank to disable pickup.')
m.step(3,'Test with a small export.','Keep the app running, export a new CSV, and check that it appears as an unreviewed draft. The app waits for a stable file before intake.')
m.h2('Set the store’s normal defaults.')
m.table(['Default','What to choose'],[
('Cash / store-credit offer %','Use the store’s buying policy. These values calculate suggested offers, not payouts.'),
('Sift threshold ($)','Use the minimum proposed retail value for deciding which bulk rows to include.'),
('Zebra resolution','Choose the actual 203 or 300 DPI of this printer; do not guess from the model name alone.')],[170,W-170],size=10.5)
m.h2('Change the admin password')
m.para('Unlock Admin, expand <b>Change admin password</b>, enter a new password of at least 12 characters, and choose <b>Change password & lock</b>. All admin sessions are locked. There is no browser reset bypass; contact the person responsible for the workstation if the password is lost.')
m.para('Keep the station’s Windows account and its saved files private. Never put a token or password in receipt notes, listing sheets or a support screenshot.',size=10.3,color=MUTED)
# 19 — Persistence.
m.new(19,'Saved work','Files, closing & backups','The application folder contains both the software and your local working records.')
m.table(['Where to look','What you will find'],[
('Links below receipt actions','Label PDF, Zebra ZPL, accepted-buy record, listing review CSV and/or storage ticket.'),
('<b>outputs</b> folder','Generated documents. Label runs use new filenames; older PDFs are not reformatted automatically.'),
('<b>station_data</b> folder','Receipt history, receiving journal, settings, password record and encrypted credential. Do not delete it.')],[165,W-165],size=10.5)
m.h2('End-of-day handoff')
m.bullet('Save and wait for <b>All changes saved</b>. Leave useful notes and keep each stack matched to its receipt ID.')
m.bullet('Resolve or flag any receiving errors. Tell the next operator about cards waiting for review, labels or a physical pull.')
m.bullet('Closing Chrome leaves folder pickup running. To stop, save, close Chrome and run <b>Stop Roca.vbs</b>. Confirm and wait for the stopped message; active work finishes first.')
m.h2('Backup and update')
m.para('For updates, save your work and close the Roca browser. Run <b>Update Roca.vbs</b> → <b>Update &amp; reopen Roca</b>. It stops Roca, backs up your files, installs the update and reopens Chrome.')
m.para('Older installation? Extract the small automatic updater ZIP, run <b>Update Roca.cmd</b> and choose the existing app folder. Close the old command window if asked. Backups are in <b>backups</b>. Keep the same Windows account for the saved token.')
m.callout('Back up the records, not just the generated labels.','Losing station_data loses receipt history and its protection against receiving a completed receipt again. Ask an admin before moving, replacing or deleting these folders.','care')
# 20 — General troubleshooting.
m.new(20,'Troubleshooting','If something goes wrong','Start with the symptom. Keep the saved receipt and read the message before retrying.')
m.table(['What you see','What to do'],[
('The app will not start','Run <b>Start Roca.vbs</b>. Read any error dialog and <b>station_data/station.log</b>. Run the current <b>Install dependencies.cmd</b> if prompted.'),
('An older station / port-in-use message','Use <b>Stop Roca</b> from the running folder. For a console-based version, close its Roca/Python window. Then start the current folder.'),
('An old layout or Edge opens','Verify you launched the current <b>Start Roca.vbs</b>. Confirm Chrome is installed. Open a newly generated label PDF, not an older file.'),
('A button is disabled','Include at least one card and finish review. Trade-ins need an accepted buy before retail labels or receiving. Committed cards may be locked.'),
('“This receipt changed in another window”','Do not keep editing both copies. Save needed notes separately, reopen the latest receipt and let one operator finish it.'),
('The export does not appear','Check that the station is running, the file is CSV, and the saved export folder is correct. Read the folder status; try manual import if needed.'),
('No admin password seems to work','There is no default password. Ask the person who created it. Do not delete station_data to reset a password.'),
('Installer fails building a package','Use the complete current release and its installer. Version 2.0.1 removes the Windows cryptography dependency. Give your admin the Python version and final error lines.')],[150,W-150],size=10)
m.callout('Make a useful support note.','Record the app version, receipt ID, action you chose and exact error. A screenshot can help—but keep tokens and passwords out of it.','note')
# 21 — Square + physical label troubleshooting.
m.new(21,'Troubleshooting','Square & label troubleshooting','Fix the cause rather than creating another receipt.')
m.table(['Problem','Next step'],[
('Nothing was added to Square','Check the action: <b>Labels only</b>, <b>Prepare only</b> and accepting a buy do not receive stock. For new stock, use the receiving action.'),
('Location is not active / accessible','Ask an admin to test the connection, choose the correct Production/Sandbox environment, select an active store and save.'),
('Token rejected / permission denied','Admin should check that the token is valid for that merchant and environment and has the required access. Operators should not replace credentials.'),
('CSV and Square prices differ','Confirm the intended retail price. Correct the CSV/receipt or update Square as appropriate. Existing Square prices are not silently overwritten.'),
('Multiple Square variations share a SKU','Stop receiving that receipt. Ask an admin to reconcile the duplicate Square entries before retrying.'),
('Receiving says NEEDS ATTENTION','Use the same receipt’s saved retry when appropriate. Persistent or old operations require checking Square before further action.'),
('QR will not scan / bars are clipped','Print at 100% on 2″ × 1″ stock, choose the correct DPI, and keep code margins clear. Confirm the scanner reads QR and selects the right Square variation.'),
('Label is rotated, tiny or cut off','Check printer stock size, orientation and scaling. Test one label. Use a fresh PDF, and avoid folding through the code.')],[150,W-150],size=10)
m.para('For version-1 stock, an existing 12-digit <b>Square SKU</b> can be supplied in the CSV or editor to retain that item’s original code. Ask an admin before remapping old stock.',size=10.3,color=MUTED)
# 22 — Desk reference.
m.new(22,'Print this page','One-page desk reference','Keep this page near the station for quick decisions.')
m.table(['I want to…','Use…'],[
('Find value in an unsorted stack','<b>Bulk scan / sift</b> → threshold → review → move the same receipt.'),
('Make a customer an offer','<b>Walk-in trade-ins</b> → review → agree → record accepted cash/credit offer.'),
('Add newly arrived copies and print','<b>Price & label</b> → <b>Add new copies</b> → <b>Receive & create labels</b>.'),
('Print for stock we already own','<b>Do not add inventory</b> → <b>Create QR labels</b>, or use <b>Labels only</b>.'),
('Prepare online stock information','<b>Online preparation</b> → channel + storage → preparation files. Publish separately.'),
('Pull an online order from a case','<b>Activity & orders</b> → <b>Record online order</b> → remove cards → <b>Confirm pulled</b>.'),
('Start a fresh stack / continue a stack','<b>New batch</b> for a fresh import. <b>Move receipt to…</b> to keep the same receipt.'),
('Retry an interrupted stock receipt','Open the same receipt → read error → <b>Retry saved receiving</b> when offered.')],[200,W-200],size=10.2)
m.h2('Before the final click')
for text in ['Physical copies match the included quantity.','Name, set, number, condition and exact finish are checked.','The intended price is confirmed for this variant.','The action matches the job: receive, label only, or prepare only.','Cards will be stored in their recorded location.']:
    y=m.y;c.setStrokeColor(MUTED);c.roundRect(M,y-10,8,8,1,stroke=1,fill=0)
    m.para(text,x=M+19,width=W-19,size=10.5,leading=14.7,space=7)
m.para('<b>Printing:</b> 2″ × 1″ · correct DPI · Actual size / 100% · QR flat on the back of a sleeve.',size=10.3,color=TEAL)
m.para('<b>Start:</b> Start Roca.vbs · <b>Stop:</b> Stop Roca.vbs. Closing Chrome leaves Roca running.',size=10.3,color=MUTED)
# 23 — Fillable station profile.
m.new(23,'Store handoff','Your store’s station profile','Fill this in with your manager. Keep credentials somewhere else.')
fields=[('Store / team name','store_name'),('Manager or admin contact','manager_contact'),('Station computer / Windows user','station_computer'),('Roca export folder','export_folder'),('Receiving Square store name','receiving_store'),('Zebra printer name / resolution','printer_details'),('Normal cash / credit offer policy','offer_policy'),('Display / bin naming convention','storage_convention')]
for label,name in fields:
    c.setFont('Bold',10.5);c.setFillColor(INK);c.drawString(M,m.y,label);m.y-=31
    c.acroForm.textfield(name=name,tooltip=label,x=M,y=m.y,width=W,height=25,fontName='Helvetica',fontSize=10,
                        borderStyle='solid',borderWidth=.6,borderColor=LINE,fillColor=colors.HexColor('#F8FAFD'),textColor=INK,forceBorder=True)
    m.y-=22
m.callout('Do not write a token or password on this page.','The admin token belongs in the app’s protected settings. Use your store’s approved process for sharing or recovering admin access.','care')
m.para('You can fill the fields in a compatible PDF reader or write on a printed copy. For the current project documentation, visit <link href="https://github.com/thundergamescda/RocaSifter" color="#315DEA">github.com/thundergamescda/RocaSifter</link>.',size=9.5,leading=13.5,color=MUTED)
m.finish()
# Publish the desk sheet and cover preview from the same finished source.
import fitz
with fitz.open(OUTPUT) as doc:
    quick=fitz.open();quick.insert_pdf(doc,from_page=21,to_page=21)
    quick.set_metadata({'title':'Roca Station — Operator Desk Reference','author':'Roca Station',
                        'subject':'Quick reference from Operator Manual '+VERSION+', page 22'})
    quick.save(str(ROOT/'docs'/'Roca_Station_Desk_Reference.pdf'),garbage=4,deflate=True)
    doc[0].get_pixmap(matrix=fitz.Matrix(1.25,1.25)).save(str(ASSETS/'manual-cover.png'))
print('Built',OUTPUT)
