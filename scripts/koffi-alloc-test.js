const koffi = require("koffi");
const user32 = koffi.load("user32.dll");
const RECT = koffi.struct("RECT", { left: "long", top: "long", right: "long", bottom: "long" });

const ptr = koffi.alloc(RECT, 1);
console.log("alloc ptr:", ptr, "typeof:", typeof ptr);
koffi.encode(ptr, RECT, { left: 10, top: 20, right: 30, bottom: 40 });
const d1 = koffi.decode(ptr, RECT);
console.log("after encode:", JSON.stringify(d1));
koffi.encode(ptr, RECT, { left: 99, top: 20, right: 30, bottom: 40 });
const d2 = koffi.decode(ptr, RECT);
console.log("after 2nd encode left:", d2.left, "(expect 99)");

// native 写入测试
const GetClientRect = user32.func("__stdcall", "GetClientRect", "int32", ["void *", "void *"]);
const GetDesktopWindow = user32.func("__stdcall", "GetDesktopWindow", "void *", []);
const desktop = GetDesktopWindow();
const ptr2 = koffi.alloc(RECT, 1);
koffi.encode(ptr2, RECT, { left: 0, top: 0, right: 0, bottom: 0 });
GetClientRect(desktop, ptr2);
const d3 = koffi.decode(ptr2, RECT);
console.log("native GetClientRect write, right:", d3.right, "(expect >0)");
