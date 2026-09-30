// Player pictures: [category, "emoji search words, ..."] - English and Danish words so kids can search in both.
// Each line is "emoji word word word"; lines are separated by ";".
const ICON_SETS = [
  ["😀", "Faces", "Ansigter", `
😀 smile happy glad smil;😃 grin happy glad;😄 laugh happy glad grine;😁 grin teeth tænder;😆 laugh lol grine;😅 sweat phew puha;
🤣 rofl laugh grine;😂 tears joy laugh grine;🙂 smile smil;🙃 upside down silly fjollet;😉 wink blink;😊 blush smile rødme;
😇 angel halo engel;🥰 love hearts kærlighed;😍 heart eyes love forelsket;🤩 star struck wow stjerne;😘 kiss kys;😋 yum tasty lækker;
😛 tongue tunge;😜 wink tongue silly fjollet;🤪 crazy zany skør;🤑 money rich penge rig;🤗 hug kram;🤭 oops giggle fnise;
🤫 shh quiet stille;🤔 think hmm tænke;🤐 zip quiet lynlås;🤨 eyebrow suspicious mistænksom;😐 neutral;😏 smirk smart;
😌 relieved calm rolig;😴 sleep tired sove træt;🤤 drool savle;😷 mask sick syg maske;🤓 nerd glasses smart klog briller;😎 cool sunglasses solbriller sej;
🥸 disguise spy spion forklædning;🧐 monocle detective detektiv;😕 confused forvirret;😮 wow surprised overrasket;😲 astonished chok;😳 flushed embarrassed;
🥺 pleading puppy eyes hundeøjne;😢 cry sad græde ked;😭 sob cry græde;😱 scream scared bange skrig;😤 huff proud stolt;😡 angry vred sur;
🤯 mind blown eksplodere;🥳 party fest fødselsdag birthday;🥶 cold frozen kold frossen;🥵 hot varm;🤠 cowboy hat;🤡 clown klovn;
👻 ghost spøgelse boo;👽 alien rumvæsen ufo;👾 space invader game spil monster;🤖 robot;💩 poop lort bæ;🎃 pumpkin halloween græskar;
😺 cat smile kat;😸 cat grin kat;😻 cat love kat;😼 cat smirk kat;🙀 cat scared kat;😹 cat laugh kat;
👹 ogre monster trold;👺 goblin monster nisse;💀 skull skelet kranie;☠️ pirate skull pirat;🫠 melting smelte;🫡 salute honnør`],
  ["🧑", "People", "Mennesker", `
👶 baby;🧒 child kid barn;👦 boy dreng;👧 girl pige;🧑 person;👱 blond hair lyst hår;
👨 man mand;👩 woman kvinde;🧔 beard skæg man mand;👴 grandpa old man bedstefar morfar farfar;👵 grandma old woman bedstemor mormor farmor;👲 cap hue;
👳 turban;🧕 headscarf tørklæde;👮 police politi betjent;👷 builder construction håndværker;💂 guard vagt;🕵️ detective spy detektiv spion;
🧑‍⚕️ doctor nurse læge sygeplejerske;🧑‍🌾 farmer landmand bonde;🧑‍🍳 cook chef kok;🧑‍🎓 student graduate student;🧑‍🎤 singer rock sanger;🧑‍🏫 teacher lærer;
🧑‍💻 coder computer programmør;🧑‍🔬 scientist forsker videnskab;🧑‍🎨 artist painter kunstner maler;🧑‍🚒 firefighter brandmand;🧑‍✈️ pilot;🧑‍🚀 astronaut space rum;
🧑‍⚖️ judge dommer;👸 princess prinsesse;🤴 prince prins;🫅 royal king queen konge dronning;🦸 superhero helt;🦹 villain skurk;
🧙 wizard mage troldmand heks;🧚 fairy fe;🧛 vampire vampyr;🧜 mermaid havfrue;🧝 elf alf;🧞 genie ånd;
🧟 zombie;🥷 ninja;🎅 santa julemand;🤶 mrs claus julemor;🧌 troll trold;🤺 fencer fægter sword sværd;
🏃 runner løber;💃 dancer danser;🕺 dance danse;🧘 yoga zen;🏄 surfer surf;🏊 swimmer svømmer;
🚴 cyclist cykel;⛷️ skier ski;🏂 snowboard;🧗 climber klatrer;🤹 juggler jonglør;🙋 raise hand hej hello`],
  ["🐶", "Animals", "Dyr", `
🐶 dog puppy hund hvalp;🐱 cat kitten kat killing;🐭 mouse mus;🐹 hamster;🐰 rabbit bunny kanin;🦊 fox ræv;
🐻 bear bjørn;🐼 panda;🐻‍❄️ polar bear isbjørn;🐨 koala;🐯 tiger;🦁 lion løve;
🐮 cow ko;🐷 pig gris;🐸 frog frø;🐵 monkey abe;🙈 see no evil monkey abe;🐔 chicken høne kylling;
🐧 penguin pingvin;🐦 bird fugl;🐤 chick kylling;🦆 duck and;🦅 eagle ørn;🦉 owl ugle;
🦇 bat flagermus;🐺 wolf ulv;🐗 boar vildsvin;🐴 horse hest;🦄 unicorn enhjørning;🐝 bee bi;
🐛 bug caterpillar larve;🦋 butterfly sommerfugl;🐌 snail snegl;🐞 ladybug mariehøne;🐜 ant myre;🕷️ spider edderkop;
🐢 turtle skildpadde;🐍 snake slange;🦎 lizard firben;🦖 t-rex dinosaur dino;🦕 dinosaur sauropod dino;🐙 octopus blæksprutte;
🦑 squid blæksprutte;🦐 shrimp reje;🦀 crab krabbe;🐡 blowfish kuglefisk;🐠 tropical fish fisk;🐟 fish fisk;
🐬 dolphin delfin;🐳 whale hval;🦈 shark haj;🐊 crocodile krokodille;🐅 tiger;🐆 leopard;
🦓 zebra;🦍 gorilla;🦧 orangutan;🐘 elephant elefant;🦛 hippo flodhest;🦏 rhino næsehorn;
🐪 camel kamel;🦒 giraffe giraf;🦘 kangaroo kænguru;🦬 bison;🐃 buffalo bøffel;🐑 sheep får lam;
🦙 llama lama;🐐 goat ged;🦌 deer hjort rudolf;🐕 dog hund;🐩 poodle puddel;🐈 cat kat;
🐈‍⬛ black cat sort kat;🐓 rooster hane;🦃 turkey kalkun;🦚 peacock påfugl;🦜 parrot papegøje;🦢 swan svane;
🦩 flamingo;🕊️ dove due;🐇 rabbit kanin hare;🦝 raccoon vaskebjørn;🦨 skunk stinkdyr;🦡 badger grævling;
🦫 beaver bæver;🦦 otter odder;🦥 sloth dovendyr;🐁 mouse mus;🐿️ squirrel egern;🦔 hedgehog pindsvin;
🐉 dragon drage;🐲 dragon drage;🦭 seal sæl;🪼 jellyfish vandmand;🦤 dodo;🦣 mammoth mammut;
🐾 paw prints poter;🪲 beetle bille;🦂 scorpion skorpion;🦟 mosquito myg;🪿 goose gås;🫎 moose elg`],
  ["🌸", "Nature", "Natur", `
🌸 cherry blossom flower blomst;🌺 hibiscus flower blomst;🌻 sunflower solsikke;🌹 rose;🌷 tulip tulipan;🌼 daisy blomst;
💐 bouquet buket;🌱 sprout spire;🌲 tree pine gran træ;🌳 tree træ;🌴 palm palme;🌵 cactus kaktus;
🍀 clover luck held kløver;🍁 maple leaf blad;🍄 mushroom svamp;🌾 wheat hvede;🪴 plant potteplante;🌰 chestnut kastanje;
🌍 earth world jorden;🌙 moon måne;🌛 moon face måne;⭐ star stjerne;🌟 glowing star stjerne;✨ sparkles glimmer;
⚡ lightning lyn;🔥 fire ild flamme;🌈 rainbow regnbue;☀️ sun sol;🌤️ sunny sol;⛅ cloud sky;
🌧️ rain regn;⛈️ storm torden;❄️ snowflake snefnug;☃️ snowman snemand;⛄ snowman snemand;🌊 wave bølge hav;
💧 drop dråbe;🌪️ tornado;🌋 volcano vulkan;🏔️ mountain bjerg;🏝️ island ø;🪐 planet saturn;
☄️ comet komet;🌌 galaxy galakse;🌠 shooting star stjerneskud;💎 gem diamond diamant;🪨 rock sten;🐚 shell muslingeskal`],
  ["🍕", "Food", "Mad", `
🍎 apple æble;🍐 pear pære;🍊 orange appelsin;🍋 lemon citron;🍌 banana banan;🍉 watermelon vandmelon;
🍇 grapes vindruer;🍓 strawberry jordbær;🫐 blueberry blåbær;🍒 cherry kirsebær;🍑 peach fersken;🥭 mango;
🍍 pineapple ananas;🥥 coconut kokosnød;🥝 kiwi;🍅 tomato tomat;🥑 avocado;🥕 carrot gulerod;
🌽 corn majs;🥦 broccoli;🥔 potato kartoffel;🍞 bread brød;🥐 croissant;🥨 pretzel kringle;
🧀 cheese ost;🥚 egg æg;🍳 fried egg spejlæg;🥞 pancakes pandekager;🧇 waffle vaffel;🥓 bacon;
🍔 burger;🍟 fries pommes frites;🍕 pizza;🌭 hotdog pølse;🥪 sandwich;🌮 taco;
🌯 burrito;🍝 spaghetti pasta;🍜 noodles nudler;🍣 sushi;🍤 shrimp reje;🍙 rice ball ris;
🥟 dumpling;🍦 ice cream is;🍧 shaved ice is;🍨 ice cream is;🍩 donut doughnut;🍪 cookie småkage;
🎂 birthday cake fødselsdag kage;🍰 cake kage;🧁 cupcake muffin;🥧 pie tærte;🍫 chocolate chokolade;🍬 candy slik;
🍭 lollipop slikkepind;🍮 pudding;🍯 honey honning;🍿 popcorn;🧃 juice;🥤 soda sodavand;
🧋 bubble tea;☕ coffee kaffe;🍵 tea te;🥛 milk mælk;🍼 bottle sutteflaske;🧊 ice is terning`],
  ["⚽", "Sport & fun", "Sport & sjov", `
⚽ soccer football fodbold;🏀 basketball;🏈 american football;⚾ baseball;🥎 softball;🎾 tennis;
🏐 volleyball;🏉 rugby;🥏 frisbee;🎱 pool billiard;🏓 ping pong bordtennis;🏸 badminton;
🏒 hockey;🥅 goal mål;⛳ golf;🏹 bow arrow bue pil;🎣 fishing fiske;🤿 diving dykke;
🥊 boxing boksning;🥋 karate judo;🛹 skateboard;🛼 roller skate rulleskøjte;⛸️ ice skate skøjte;🎿 ski;
🛷 sled slæde kælk;🏆 trophy pokal vinder winner;🥇 gold medal guld;🥈 silver medal sølv;🥉 bronze medal bronze;🏅 medal medalje;
🎖️ medal medalje;🎯 target dart bullseye;🎮 video game controller spil;🕹️ joystick arcade;🎲 dice terning;🧩 puzzle;
♟️ chess pawn skak bonde;🃏 joker card kort;🎴 cards kort;🀄 mahjong;🧸 teddy bear bamse;🪀 yoyo;
🪁 kite drage;🎨 art paint kunst maling;🎭 theater masks teater;🎪 circus cirkus;🎤 microphone sing mikrofon;🎧 headphones høretelefoner;
🎸 guitar;🎹 piano klaver;🥁 drum tromme;🎺 trumpet trompet;🎻 violin;🎷 saxophone saxofon;
🪗 accordion harmonika;🎵 music note musik;🎬 movie film;📚 books bøger;✏️ pencil blyant;🔬 microscope mikroskop;
🔭 telescope teleskop;🧪 test tube science kemi;🎁 gift present gave;🎈 balloon ballon;🎉 party fest;🎊 confetti konfetti;
🪄 magic wand tryllestav;🔮 crystal ball krystalkugle;🧿 evil eye;🪅 pinata;🪩 disco ball;🎠 carousel karrusel`],
  ["🚀", "Travel", "Rejser", `
🚀 rocket raket;🛸 ufo flying saucer;🛰️ satellite satellit;✈️ airplane fly;🚁 helicopter helikopter;🪂 parachute faldskærm;
⛵ sailboat sejlbåd;🚤 speedboat båd;🛶 canoe kano;⚓ anchor anker;🚢 ship skib;🏴‍☠️ pirate flag pirat;
🚗 car bil;🚕 taxi;🚙 suv bil;🚌 bus;🏎️ race car racerbil;🚓 police car politibil;
🚑 ambulance;🚒 fire truck brandbil;🚜 tractor traktor;🚚 truck lastbil;🏍️ motorcycle motorcykel;🛵 scooter;
🚲 bicycle bike cykel;🛴 kick scooter løbehjul;🚂 train steam tog damptog;🚄 fast train tog;🚇 metro subway;🚡 cable car svævebane;
🎡 ferris wheel pariserhjul;🎢 roller coaster rutsjebane;🏰 castle slot borg;🏯 japanese castle slot;🗼 tower tårn;🗽 statue liberty frihedsgudinden;
🏠 house home hus;🏡 house garden hus have;⛺ tent telt camping;🏕️ camping;🗺️ map kort;🧭 compass kompas;
🌆 city by;🌃 night city nat;🏟️ stadium stadion;⛲ fountain springvand;🗿 moai statue;🏛️ museum`],
  ["👑", "Things", "Ting", `
👑 crown king queen krone konge dronning;💍 ring;🎩 top hat magic hat tryllehat;🧢 cap kasket;👒 hat sun hat;⛑️ helmet hjelm;
🕶️ sunglasses solbriller;👓 glasses briller;🎒 backpack rygsæk;👟 sneaker sko;👠 heel sko;🧦 socks sokker;
🧣 scarf halstørklæde;🧤 gloves handsker;👗 dress kjole;👕 t-shirt;🩳 shorts;🥽 goggles;
⚔️ swords sværd;🗡️ dagger dolk;🛡️ shield skjold;🏹 bow bue;🪃 boomerang;🔨 hammer;
🪓 axe økse;🔧 wrench skruenøgle;⚙️ gear tandhjul;🧲 magnet;💡 light bulb idea idé pære;🔦 flashlight lommelygte;
🕯️ candle lys;🔑 key nøgle;🗝️ old key nøgle;🔒 lock lås;🔔 bell klokke;📯 horn;
⏰ alarm clock vækkeur;⌛ hourglass timeglas;📷 camera kamera;📱 phone telefon;💻 laptop computer;⌚ watch ur;
📦 box package pakke;✉️ letter brev;📌 pin;📎 paperclip clips;✂️ scissors saks;🖍️ crayon farvekridt;
💰 money bag penge;💵 money dollar penge;🪙 coin mønt;💸 money flying penge;🧸 teddy bamse;🪆 nesting doll;
🏺 vase amphora;⚱️ urn;🧯 extinguisher;🪣 bucket spand;🧹 broom kost heks;🧺 basket kurv;
🪞 mirror spejl;🛏️ bed seng;🚪 door dør;🪜 ladder stige;🧱 brick mursten lego;🪵 wood log træ`],
  ["❤️", "Symbols", "Symboler", `
❤️ red heart love hjerte kærlighed;🧡 orange heart hjerte;💛 yellow heart hjerte;💚 green heart hjerte;💙 blue heart hjerte;💜 purple heart hjerte;
🖤 black heart hjerte;🤍 white heart hjerte;🤎 brown heart hjerte;💖 sparkling heart hjerte;💝 heart gift hjerte;💘 cupid hjerte;
💯 hundred perfect;💥 boom collision bang;💫 dizzy star;💦 splash sprøjt;💨 dash fast hurtig;💤 sleep zzz;
💬 speech chat;💭 thought tanke;🌀 cyclone spiral;♻️ recycle genbrug;☮️ peace fred;☯️ yin yang;
✅ check yes ja;❌ cross no nej;❓ question spørgsmål;❗ exclamation;⚠️ warning advarsel;🚫 no forbidden forbudt;
🔴 red circle rød;🟠 orange circle;🟡 yellow circle gul;🟢 green circle grøn;🔵 blue circle blå;🟣 purple circle lilla;
⚫ black circle sort;⚪ white circle hvid;🟥 red square rød;🟦 blue square blå;🟩 green square grøn;🟨 yellow square gul;
🔶 orange diamond;🔷 blue diamond;🔺 triangle trekant;♠️ spade spar;♥️ heart hjerter;♦️ diamond ruder;
♣️ club klør;♔ white king chess skak konge;♕ white queen chess skak dronning;♖ rook tårn skak;♗ bishop løber skak;♘ knight springer hest skak;
☢️ radioactive;☣️ biohazard;♾️ infinity uendelig;🆒 cool sej;🆕 new ny;🆗 ok;
🔝 top;🎦 cinema;🏁 checkered flag race mål;🚩 red flag flag;🏳️‍🌈 rainbow flag pride regnbue;🇩🇰 denmark danmark dannebrog flag`],
];

// flatten once: [{ e, words, cat }]; an emoji listed in two categories keeps the first and gets both sets of words
const ICONS = [...ICON_SETS.flatMap(([cat, en, da, body]) => body.split(";").map((s) => s.trim()).filter(Boolean).map((s) => {
  const i = s.indexOf(" ");
  return { e: s.slice(0, i), name: s.slice(i + 1), words: `${en} ${da} ${s.slice(i + 1)}`.toLowerCase(), cat };
})).reduce((m, x) => { const o = m.get(x.e); o ? (o.words += " " + x.words) : m.set(x.e, x); return m; }, new Map()).values()];
