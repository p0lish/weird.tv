let CHANAPI = (() => {
    let CHANAPI = {
        url: "",
        catalogData: "",
        init: function (board) {
            this.url = "http://a.4cdn.org/" + board + "/catalog.json";
        },


        getCatalog: function () {
            console.log("read data from", this.url);
            getJSONP(this.url, function(data) {
                console.log(data);
            });
        },

    };
    return CHANAPI;
})()