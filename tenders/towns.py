TOWNS_BY_COUNTRY = {
    'Tanzania': [
        'Dar es Salaam', 'Mwanza', 'Arusha', 'Dodoma', 'Mbeya', 'Zanzibar',
        'Tanga', 'Morogoro', 'Tabora', 'Kigoma', 'Mtwara', 'Musoma',
        'Iringa', 'Moshi', 'Singida', 'Songea', 'Babati', 'Sumbawanga',
        'Kahama', 'Shinyanga', 'Bukoba', 'Njombe', 'Geita', 'Mpanda',
        'Kibaha', 'Bagamoyo',
    ],
    'Zambia': [
        'Lusaka', 'Kitwe', 'Ndola', 'Livingstone', 'Kabwe', 'Chipata',
        'Mufulira', 'Chingola', 'Luanshya', 'Kasama', 'Solwezi', 'Mongu',
        'Mansa', 'Choma', 'Mazabuka', 'Kafue', 'Nakonde', 'Mpika',
        'Kapiri Mposhi',
    ],
    'Congo': [
        'Kinshasa', 'Lubumbashi', 'Goma', 'Bukavu', 'Kisangani', 'Kananga',
        'Mbuji-Mayi', 'Kolwezi', 'Likasi', 'Matadi', 'Kikwit', 'Uvira',
        'Beni', 'Kindu', 'Mbandaka', 'Isiro', 'Boma',
    ],
    'Burundi': [
        'Bujumbura', 'Gitega', 'Ngozi', 'Muyinga', 'Rumonge', 'Ruyigi',
        'Cibitoke', 'Bururi', 'Kayanza', 'Makamba', 'Muramvya',
    ],
    'Rwanda': [
        'Kigali', 'Huye', 'Rubavu', 'Musanze', 'Rusizi', 'Karongi',
        'Muhanga', 'Gicumbi', 'Rwamagana', 'Nyagatare', 'Bugesera',
    ],
    'Kenya': [
        'Nairobi', 'Mombasa', 'Kisumu', 'Nakuru', 'Eldoret', 'Thika',
        'Malindi', 'Kitale', 'Garissa', 'Nyeri', 'Kakamega', 'Kericho',
        'Naivasha', 'Machakos', 'Isiolo', 'Lodwar', 'Mandera', 'Wajir',
    ],
    'South Sudan': [
        'Juba', 'Wau', 'Malakal', 'Bor', 'Yei', 'Yambio', 'Aweil',
        'Rumbek', 'Bentiu', 'Torit', 'Kapoeta', 'Nimule', 'Renk',
    ],
    'Uganda': [
        'Kampala', 'Entebbe', 'Gulu', 'Mbale', 'Jinja', 'Mbarara',
        'Masaka', 'Lira', 'Soroti', 'Arua', 'Fort Portal', 'Kabale',
        'Mukono', 'Kasese', 'Moroto', 'Tororo', 'Busia', 'Kotido',
    ],
}

TOWN_CHOICES = [
    (town, town)
    for country_towns in TOWNS_BY_COUNTRY.values()
    for town in country_towns
]